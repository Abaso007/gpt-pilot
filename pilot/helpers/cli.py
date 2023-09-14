import subprocess
import os
import signal
import threading
import queue
import time
import uuid
import platform

from termcolor import colored
from database.database import get_command_run_from_hash_id, save_command_run
from const.function_calls import DEBUG_STEPS_BREAKDOWN

from utils.questionary import styled_text
from const.code_execution import MAX_COMMAND_DEBUG_TRIES, MIN_COMMAND_RUN_TIME, MAX_COMMAND_RUN_TIME, MAX_COMMAND_OUTPUT_LENGTH

interrupted = False

def enqueue_output(out, q):
    for line in iter(out.readline, ''):
        if interrupted:  # Check if the flag is set
            break
        q.put(line)
    out.close()

def run_command(command, root_path, q_stdout, q_stderr, pid_container):
    """
    Execute a command in a subprocess.

    Args:
        command (str): The command to run.
        root_path (str): The directory in which to run the command.
        q_stdout (Queue): A queue to capture stdout.
        q_stderr (Queue): A queue to capture stderr.
        pid_container (list): A list to store the process ID.

    Returns:
        subprocess.Popen: The subprocess object.
    """
    if platform.system() == 'Windows':  # Check the operating system
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=root_path
        )
    else:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,  # Use os.setsid only for Unix-like systems
            cwd=root_path
        )

    pid_container[0] = process.pid
    t_stdout = threading.Thread(target=enqueue_output, args=(process.stdout, q_stdout))
    t_stderr = threading.Thread(target=enqueue_output, args=(process.stderr, q_stderr))
    t_stdout.daemon = True
    t_stderr.daemon = True
    t_stdout.start()
    t_stderr.start()
    return process

def terminate_process(pid):
    if platform.system() == "Windows":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)])
        except subprocess.CalledProcessError:
            # Handle any potential errors here
            pass
    else:  # Unix-like systems
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            # Handle any potential errors here
            pass

def execute_command(project, command, timeout=None, force=False):
    """
    Execute a command and capture its output.

    Args:
        project: The project associated with the command.
        command (str): The command to run.
        timeout (int, optional): The maximum execution time in milliseconds. Default is None.
        force (bool, optional): Whether to execute the command without confirmation. Default is False.

    Returns:
        str: The command output.
    """
    if timeout is not None:
        if timeout < 1000:
            timeout *= 1000
        timeout = min(max(timeout, MIN_COMMAND_RUN_TIME), MAX_COMMAND_RUN_TIME)

    if not force:
        print(colored(f'\n--------- EXECUTE COMMAND ----------', 'yellow', attrs=['bold']))
        print(
            colored('Can i execute the command: `')
            + colored(command, 'yellow', attrs=['bold'])
            + colored(f'` with {timeout}ms timeout?')
        )

        answer = styled_text(
            project,
            'If yes, just press ENTER'
        )


    # TODO when a shell built-in commands (like cd or source) is executed, the output is not captured properly - this will need to be changed at some point
    if "cd " in command or "source " in command:
        command = f"bash -c '{command}'"


    project.command_runs_count += 1
    command_run = get_command_run_from_hash_id(project, command)
    if command_run is not None and project.skip_steps:
        # if we do, use it
        project.checkpoints['last_command_run'] = command_run
        print(colored(f'Restoring command run response id {command_run.id}:\n```\n{command_run.cli_response}```', 'yellow'))
        return command_run.cli_response

    return_value = None

    q_stderr = queue.Queue()
    q = queue.Queue()
    pid_container = [None]
    process = run_command(command, project.root_path, q, q_stderr, pid_container)
    output = ''
    stderr_output = ''
    start_time = time.time()
    interrupted = False

    try:
        while return_value is None:
            elapsed_time = time.time() - start_time
            if timeout is not None:
                print(colored(f'\rt: {round(elapsed_time * 1000)}ms : ', 'white', attrs=['bold']), end='', flush=True)

            # Check if process has finished
            if process.poll() is not None:
                # Get remaining lines from the queue
                time.sleep(0.1) # TODO this shouldn't be used
                while not q.empty():
                    output_line = q.get_nowait()
                    if output_line not in output:
                        print(colored('CLI OUTPUT:', 'green') + output_line, end='')
                        output += output_line
                break

            # If timeout is reached, kill the process
            if timeout is not None and elapsed_time * 1000 > timeout:
                raise TimeoutError("Command exceeded the specified timeout.")
                # os.killpg(pid_container[0], signal.SIGKILL)
                # break

            try:
                line = q.get_nowait()
            except queue.Empty:
                line = None

            if line:
                output += line
                print(colored('CLI OUTPUT:', 'green') + line, end='')

            # Read stderr
            try:
                stderr_line = q_stderr.get_nowait()
            except queue.Empty:
                stderr_line = None

            if stderr_line:
                stderr_output += stderr_line
                print(colored('CLI ERROR:', 'red') + stderr_line, end='')  # Print with different color for distinction

    except (KeyboardInterrupt, TimeoutError) as e:
        interrupted = True
        if isinstance(e, KeyboardInterrupt):
            print("\nCTRL+C detected. Stopping command execution...")
        else:
            print("\nTimeout detected. Stopping command execution...")

        terminate_process(pid_container[0])

    # stderr_output = ''
    # while not q_stderr.empty():
    #     stderr_output += q_stderr.get_nowait()

    if return_value is None:
        return_value = ''
        if stderr_output != '':
            return_value = 'stderr:\n```\n' + stderr_output[-MAX_COMMAND_OUTPUT_LENGTH:] + '\n```\n'
        return_value += 'stdout:\n```\n' + output[-MAX_COMMAND_OUTPUT_LENGTH:] + '\n```'

    command_run = save_command_run(project, command, return_value)

    return return_value

def build_directory_tree(path, prefix="", ignore=None, is_last=False, files=None, add_descriptions=False):
    """Build the directory tree structure in tree-like format.

    Args:
    - path: The starting directory path.
    - prefix: Prefix for the current item, used for recursion.
    - ignore: List of directory names to ignore.
    - is_last: Flag to indicate if the current item is the last in its parent directory.

    Returns:
    - A string representation of the directory tree.
    """
    if ignore is None:
        ignore = []

    if os.path.basename(path) in ignore:
        return ""

    output = ""
    indent = '|   ' if not is_last else '    '

    if os.path.isdir(path):
        # It's a directory, add its name to the output and then recurse into it
        output += (
            f"{prefix}|-- {os.path.basename(path)}"
            + (
                f' - {files[os.path.basename(path)].description} '
                if files
                and os.path.basename(path) in files
                and add_descriptions
                else ''
            )
            + "/\n"
        )

        # List items in the directory
        items = os.listdir(path)
        for index, item in enumerate(items):
            item_path = os.path.join(path, item)
            output += build_directory_tree(item_path, prefix + indent, ignore, index == len(items) - 1, files, add_descriptions)

    else:
        # It's a file, add its name to the output
        output += (
            f"{prefix}|-- {os.path.basename(path)}"
            + (
                f' - {files[os.path.basename(path)].description} '
                if files
                and os.path.basename(path) in files
                and add_descriptions
                else ''
            )
            + "\n"
        )

    return output

def execute_command_and_check_cli_response(command, timeout, convo):
    """
    Execute a command and check its CLI response.

    Args:
        command (str): The command to run.
        timeout (int): The maximum execution time in milliseconds.
        convo (AgentConvo): The conversation object.

    Returns:
        tuple: A tuple containing the CLI response and the agent's response.
    """
    cli_response = execute_command(convo.agent.project, command, timeout)
    response = convo.send_message('dev_ops/ran_command.prompt',
        { 'cli_response': cli_response, 'command': command })
    return cli_response, response

def run_command_until_success(command, timeout, convo, additional_message=None, force=False):
    """
    Run a command until it succeeds or reaches a timeout.

    Args:
        command (str): The command to run.
        timeout (int): The maximum execution time in milliseconds.
        convo (AgentConvo): The conversation object.
        additional_message (str, optional): Additional message to include in the response.
        force (bool, optional): Whether to execute the command without confirmation. Default is False.
    """
    cli_response = execute_command(convo.agent.project, command, timeout, force)
    response = convo.send_message('dev_ops/ran_command.prompt',
        {'cli_response': cli_response, 'command': command, 'additional_message': additional_message})

    if response != 'DONE':
        print(colored('Got incorrect CLI response:', 'red'))
        print(cli_response)
        print(colored('-------------------', 'red'))

        debug(convo, {'command': command, 'timeout': timeout})



def debug(convo, command=None, user_input=None, issue_description=None):
    """
    Debug a conversation.

    Args:
        convo (AgentConvo): The conversation object.
        command (dict, optional): The command to debug. Default is None.
        user_input (str, optional): User input for debugging. Default is None.
        issue_description (str, optional): Description of the issue to debug. Default is None.

    Returns:
        bool: True if debugging was successful, False otherwise.
    """
    function_uuid = str(uuid.uuid4())
    convo.save_branch(function_uuid)
    success = False

    for _ in range(MAX_COMMAND_DEBUG_TRIES):
        if success:
            break

        convo.load_branch(function_uuid)

        debugging_plan = convo.send_message('dev_ops/debug.prompt',
            { 'command': command['command'] if command is not None else None, 'user_input': user_input, 'issue_description': issue_description },
            DEBUG_STEPS_BREAKDOWN)

        # TODO refactor to nicely get the developer agent
        success = convo.agent.project.developer.execute_task(
            convo,
            debugging_plan,
            command,
            False,
            False)


    if not success:
        # TODO explain better how should the user approach debugging
        # we can copy the entire convo to clipboard so they can paste it in the playground
        user_input = convo.agent.project.ask_for_human_intervention(
            'It seems like I cannot debug this problem by myself. Can you please help me and try debugging it yourself?' if user_input is None else f'Can you check this again:\n{issue_description}?',
            command
        )

        if user_input == 'continue':
            success = True

    return success
