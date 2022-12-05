import asyncio
from dataclasses import dataclass
import os
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from io import TextIOBase
from typing import Any, Callable, List, Optional, TextIO, Tuple, Union

import anyio
import anyio.abc
from anyio.streams.text import TextReceiveStream, TextSendStream

TextSink = Union[anyio.AsyncFile, TextIO, TextSendStream]


def ctrl_c_handler(process: anyio.abc.Process):
    """
    A Windows CTRL-C handler that accepts any anyio subprocess
    and terminates it before passing control to the next handler
    """
    def handler(*args):
        # send signal using os.kill to avoid anyio's signal handling
        os.kill(process.pid, signal.CTRL_BREAK_EVENT)
        
        # return False to allow the next handler to be called
        return False

    return handler


@asynccontextmanager
async def open_process(command: List[str], **kwargs):
    """
    Like `anyio.open_process` but with:
    - Support for Windows command joining
    - Termination of the process on exception during yield
    - Forced cleanup of process resources during cancellation
    """
    # Passing a string to open_process is equivalent to shell=True which is
    # generally necessary for Unix-like commands on Windows but otherwise should
    # be avoided
    if not isinstance(command, list):
        raise TypeError(
            "The command passed to open process must be a list. You passed the command"
            f"'{command}', which is type '{type(command)}'."
        )

    # if sys.platform == "win32":
    #     command = " ".join(command)

    if sys.platform == "win32":
        import win32api
        from unittest.mock import patch
        command = " ".join(command)

        # the kwargs that anyio.open_process uses to create the process
        anyio_kwargs = ["stdout", "stderr", "cwd", "env"]
        
        # store a reference to asyncio.create_subprocess_exec to use 
        # after patching
        asyncio_create_subprocess = asyncio.create_subprocess_shell

        async def create_subprocess(command, **kwargs):
            # select only the kwargs that anyio.open_process uses to create the process
            kwargs = {k: v for k, v in kwargs.items() if k in anyio_kwargs}
            # add creationflags to kwargs
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
 
            return await asyncio_create_subprocess(
                command,
                **kwargs
            )

        with patch('asyncio.create_subprocess_shell', create_subprocess):
            process = await anyio.open_process(command, **kwargs)
            #add ctrl-c handler
            handler = win32api.SetConsoleCtrlHandler(ctrl_c_handler(process), True)
    else:
        process = await anyio.open_process(command, **kwargs)

    try:
        async with process:
            yield process
    finally:
        try:
            process.terminate()

            if sys.platform == "win32":
                import win32api
                # remove ctrl-c handler
                win32api.SetConsoleCtrlHandler(handler, False)

        except ProcessLookupError:
            # Occurs if the process is already terminated
            pass

        # Ensure the process resource is closed. If not shielded from cancellation,
        # this resource can be left open and the subprocess output can appear after
        # the parent process has exited.
        with anyio.CancelScope(shield=True):
            await process.aclose()


async def run_process(
    command: List[str],
    stream_output: Union[bool, Tuple[Optional[TextSink], Optional[TextSink]]] = False,
    task_status: Optional[anyio.abc.TaskStatus] = None,
    task_status_handler: Optional[Callable[[anyio.abc.Process], Any]] = None,
    **kwargs,
):
    """
    Like `anyio.run_process` but with:

    - Use of our `open_process` utility to ensure resources are cleaned up
    - Simple `stream_output` support to connect the subprocess to the parent stdout/err
    - Support for submission with `TaskGroup.start` marking as 'started' after the
        process has been created. When used, the PID is returned to the task status.

    """
    if stream_output is True:
        stream_output = (sys.stdout, sys.stderr)

    async with open_process(
        command,
        stdout=subprocess.PIPE if stream_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE if stream_output else subprocess.DEVNULL,
        **kwargs,
    ) as process:

        if task_status is not None:
            if not task_status_handler:
                task_status_handler = lambda process: process.pid

            task_status.started(task_status_handler(process))

        if stream_output:
            await consume_process_output(
                process, stdout_sink=stream_output[0], stderr_sink=stream_output[1]
            )

        await process.wait()

    return process


async def consume_process_output(
    process,
    stdout_sink: Optional[TextSink] = None,
    stderr_sink: Optional[TextSink] = None,
):
    async with anyio.create_task_group() as tg:
        tg.start_soon(
            stream_text,
            TextReceiveStream(process.stdout),
            stdout_sink,
        )
        tg.start_soon(
            stream_text,
            TextReceiveStream(process.stderr),
            stderr_sink,
        )


async def stream_text(source: TextReceiveStream, sink: Optional[TextSink]):
    if isinstance(sink, TextIOBase):
        # Convert the blocking sink to an async-compatible object
        sink = anyio.wrap_file(sink)

    async for item in source:
        if isinstance(sink, TextSendStream):
            await sink.send(item)
        elif isinstance(sink, anyio.AsyncFile):
            await sink.write(item)
            await sink.flush()
        elif sink is None:
            pass  # Consume the item but perform no action
        else:
            raise TypeError(f"Unsupported sink type {type(sink).__name__}")


def kill_on_interrupt(pid: int, process_name: str, print_fn: Callable):
    """Kill a process with the given `pid` when a SIGNINT is received."""

    # In a non-windows enviornment first interrupt with send a SIGTERM, then
    # subsequent interrupts will send SIGKILL. In Windows we use
    # CTRL_BREAK_EVENT as SIGTERM is useless:
    # https://bugs.python.org/issue26350
    if sys.platform == "win32":

        def stop_process(*args):
            print_fn(f"\nStopping {process_name}...")
            os.kill(pid, signal.CTRL_BREAK_EVENT)

    else:

        def stop_process(*args):
            print_fn(f"\nStopping {process_name}...")
            os.kill(pid, signal.SIGTERM)
            signal.signal(signal.SIGINT, kill_process)

        def kill_process(*args):
            print_fn(f"\nKilling {process_name}...")
            os.kill(pid, signal.SIGKILL)

    signal.signal(signal.SIGINT, stop_process)
