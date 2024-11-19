"""
Run Command with a input text.
"""

import json
import os
import shlex
import subprocess
import sys
import threading
from typing import Dict, List

from devchat.utils import get_logger

from .command_parser import Command
from .util import ToolUtil

logger = get_logger(__name__)


DEVCHAT_COMMAND_MISS_ERROR_MESSAGE = (
    "devchat-commands environment is not installed yet. "
    "Please install it before using the current command."
    "The devchat-command environment is automatically "
    "installed after the plugin starts,"
    " and details can be viewed in the output window."
)


def pipe_reader(pipe, out_data, out_flag):
    while pipe:
        data = pipe.read(1)
        if data == "":
            break
        out_data["out"] += data
        print(data, end="", file=out_flag, flush=True)


# Equivalent of CommandRun in Python\which executes subprocesses
class CommandRunner:
    def __init__(self, model_name: str):
        self.process = None
        self._model_name = model_name

    def run_command(
        self,
        command_name: str,
        command: Command,
        history_messages: List[Dict],
        input_text: str,
        parent_hash: str,
    ):
        """
        if command has parameters, then generate command parameters from input by LLM
        if command.input is "required", and input is null, then return error
        """
        input_text = (
            input_text.strip()
            .replace(f"/{command_name}", "")
            .replace('"', '\\"')
            .replace("'", "\\'")
            .replace("\n", "\\n")
        )

        arguments = {}
        if command.parameters and len(command.parameters) > 0:
            if not self._model_name.startswith("gpt-"):
                return None

            arguments = self._call_function_by_llm(command_name, command, history_messages)
            if not arguments:
                print("No valid parameters generated by LLM", file=sys.stderr, flush=True)
                return (-1, "")

        return self.run_command_with_parameters(
            command_name=command_name,
            command=command,
            parameters={"input": input_text, **arguments},
            parent_hash=parent_hash,
            history_messages=history_messages,
        )

    def run_command_with_parameters(
        self,
        command_name: str,
        command: Command,
        parameters: Dict[str, str],
        parent_hash: str,
        history_messages: List[Dict],
    ):
        """
        replace $xxx in command.steps[0].run with parameters[xxx]
        then run command.steps[0].run
        """
        result = (-1, "")
        try:
            env = os.environ.copy()
            env.update(parameters)
            env.update(self.__load_command_runtime(command))
            env.update(self.__load_chat_data(self._model_name, parent_hash, history_messages))
            self.__update_devchat_python_path(env, command.steps[0]["run"])

            command_run = command.steps[0]["run"]
            for parameter in env:
                command_run = command_run.replace("$" + parameter, str(env[parameter]))

            if self.__check_command_python_error(command_run, env):
                return result
            if self.__check_input_miss_error(command, command_name, env):
                if self.__get_readme(command):
                    result = (0, "")
                return result
            if self.__check_parameters_miss_error(command, command_run):
                if self.__get_readme(command):
                    result = (0, "")
                return result

            result = self.__run_command_with_thread_output(command_run, env)
        except Exception as err:
            print("Exception:", type(err), err, file=sys.stderr, flush=True)
            logger.exception("Run command error: %s", err)
        return result

    def __run_command_with_thread_output(self, command_str: str, env: Dict[str, str]):
        """
        run command string
        """

        def handle_output(process):
            stdout_data, stderr_data = {"out": ""}, {"out": ""}
            stdout_thread = threading.Thread(
                target=pipe_reader, args=(process.stdout, stdout_data, sys.stdout)
            )
            stderr_thread = threading.Thread(
                target=pipe_reader, args=(process.stderr, stderr_data, sys.stderr)
            )
            stdout_thread.start()
            stderr_thread.start()
            stdout_thread.join()
            stderr_thread.join()
            return (process.wait(), stdout_data["out"])

        for key in env:
            if isinstance(env[key], (List, Dict)):
                env[key] = json.dumps(env[key])
        with subprocess.Popen(
            shlex.split(command_str),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        ) as process:
            return handle_output(process)

    def __check_command_python_error(self, command_run: str, parameters: Dict[str, str]):
        need_command_python = command_run.find("$command_python ") != -1
        has_command_python = parameters.get("command_python", None)

        if need_command_python and not has_command_python:
            print(DEVCHAT_COMMAND_MISS_ERROR_MESSAGE, file=sys.stderr, flush=True)
            return True
        return False

    def __get_readme(self, command: Command):
        try:
            command_dir = os.path.dirname(command.path)
            readme_file = os.path.join(command_dir, "README.md")
            if os.path.exists(readme_file):
                with open(readme_file, "r", encoding="utf8") as file:
                    readme = file.read()
                return readme
            return None
        except Exception:
            return None

    def __check_input_miss_error(
        self, command: Command, command_name: str, parameters: Dict[str, str]
    ):
        is_input_required = command.input == "required"
        if not (is_input_required and parameters["input"] == ""):
            return False

        input_miss_error = (
            f"{command_name} workflow is missing input. Example usage: "
            f"'/{command_name} user input'\n"
        )
        readme_content = self.__get_readme(command)
        if readme_content:
            print(readme_content, flush=True)
        else:
            print(input_miss_error, file=sys.stderr, flush=True)
        return True

    def __check_parameters_miss_error(self, command: Command, command_run: str):
        # visit parameters in command
        parameter_names = command.parameters.keys() if command.parameters else []
        if len(parameter_names) == 0:
            return False

        missed_parameters = []
        for parameter_name in parameter_names:
            if command_run.find("$" + parameter_name) != -1:
                missed_parameters.append(parameter_name)

        if len(missed_parameters) == 0:
            return False

        readme_content = self.__get_readme(command)
        if readme_content:
            print(readme_content, flush=True)
        else:
            print("Missing parameters:", missed_parameters, file=sys.stderr, flush=True)
        return True

    def __load_command_runtime(self, command: Command):
        command_path = os.path.dirname(command.path)
        runtime_config = {}

        # visit each path in command_path, for example: /usr/x1/x2/x3
        # then load visit: /usr, /usr/x1, /usr/x1/x2, /usr/x1/x2/x3
        paths = command_path.split("/")
        for index in range(1, len(paths) + 1):
            try:
                path = "/".join(paths[:index])
                runtime_file = os.path.join(path, "runtime.json")
                if os.path.exists(runtime_file):
                    with open(runtime_file, "r", encoding="utf8") as file:
                        command_runtime_config = json.loads(file.read())
                        runtime_config.update(command_runtime_config)
            except Exception:
                pass

        # for windows
        if runtime_config.get("command_python", None):
            runtime_config["command_python"] = runtime_config["command_python"].replace("\\", "/")
        return runtime_config

    def __load_chat_data(self, model_name: str, parent_hash: str, history_messages: List[Dict]):
        return {
            "LLM_MODEL": model_name if model_name else "",
            "PARENT_HASH": parent_hash if parent_hash else "",
            "CONTEXT_CONTENTS": history_messages if history_messages else [],
        }

    def __update_devchat_python_path(self, env: Dict[str, str], command_run: str):
        python_path = os.environ.get("PYTHONPATH", "")
        env["DEVCHAT_PYTHONPATH"] = os.environ.get("DEVCHAT_PYTHONPATH", python_path)
        if command_run.find("$devchat_python ") == -1:
            del env["PYTHONPATH"]
        env["devchat_python"] = sys.executable.replace("\\", "/")

    def _call_function_by_llm(
        self, command_name: str, command: Command, history_messages: List[Dict]
    ):
        """
        command needs multi parameters, so we need parse each
        parameter by LLM from input_text
        """
        tools = [ToolUtil.make_function(command, command_name)]

        function_call = ToolUtil.select_function_by_llm(history_messages, tools)
        if not function_call:
            return None

        return function_call["arguments"]
