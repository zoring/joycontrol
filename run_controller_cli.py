#!/usr/bin/env python3

import argparse
import asyncio
import logging
import os
import queue
import threading
import select
import sys
import re
from aioconsole import ainput

import joycontrol.debug as debug
from joycontrol import logging_default as log, utils
from joycontrol.command_line_interface import ControllerCLI
from joycontrol.controller import Controller
from joycontrol.controller_state import ControllerState, button_push, button_press, button_release, button_clear
from joycontrol.memory import FlashMemory
from joycontrol.protocol import controller_protocol_factory
from joycontrol.server import create_hid_server
from joycontrol.nfc_tag import NFCTag

logger = logging.getLogger(__name__)

"""Emulates Switch controller. Opens joycontrol.command_line_interface to send button commands and more.

While running the cli, call "help" for an explanation of available commands.

Usage:
    run_controller_cli.py <controller> [--device_id | -d  <bluetooth_adapter_id>]
                                       [--spi_flash <spi_flash_memory_file>]
                                       [--reconnect_bt_addr | -r <console_bluetooth_address>]
                                       [--log | -l <communication_log_file>]
                                       [--nfc <nfc_data_file>]
    run_controller_cli.py -h | --help

Arguments:
    controller      Choose which controller to emulate. Either "JOYCON_R", "JOYCON_L" or "PRO_CONTROLLER"

Options:
    -d --device_id <bluetooth_adapter_id>   ID of the bluetooth adapter. Integer matching the digit in the hci* notation
                                            (e.g. hci0, hci1, ...) or Bluetooth mac address of the adapter in string
                                            notation (e.g. "FF:FF:FF:FF:FF:FF").
                                            Note: Selection of adapters may not work if the bluez "input" plugin is
                                            enabled.

    --spi_flash <spi_flash_memory_file>     Memory dump of a real Switch controller. Required for joystick emulation.
                                            Allows displaying of JoyCon colors.
                                            Memory dumps can be created using the dump_spi_flash.py script.

    -r --reconnect_bt_addr <console_bluetooth_address>  Previously connected Switch console Bluetooth address in string
                                                        notation (e.g. "FF:FF:FF:FF:FF:FF") for reconnection.
                                                        Does not require the "Change Grip/Order" menu to be opened,

    -l --log <communication_log_file>       Write hid communication (input reports and output reports) to a file.

    --nfc <nfc_data_file>                   Sets the nfc data of the controller to a given nfc dump upon initial
                                            connection.
"""

char_to_key = {
    "A": 'a',
    "B": 'b',
    "X": 'x',
    "Y": 'y',
    "a": 'a',
    "b": 'b',
    "x": 'x',
    "y": 'y',
    "+": 'plus',
    "-": 'minus',
    "r": 'r',
    "l": 'l',
    "zl":'zl',
    "zr": 'zr'
}


def l_up(controller_state):
    stick = controller_state.l_stick_state
    stick.set_up()

def l_down(controller_state):
    stick = controller_state.l_stick_state
    stick.set_down()

def l_left(controller_state):
    stick = controller_state.l_stick_state
    stick.set_left()

def l_right(controller_state):
    stick = controller_state.l_stick_state
    stick.set_right()

def r_up(controller_state):
    stick = controller_state.r_stick_state
    stick.set_up()

def r_down(controller_state):
    stick = controller_state.r_stick_state
    stick.set_down()

def r_left(controller_state):
    stick = controller_state.r_stick_state
    stick.set_left()

def r_right(controller_state):
    stick = controller_state.r_stick_state
    stick.set_right()

def extract_duration(text, default=0.3):
    """
    提取并限制时间参数
    :param text: 输入文本
    :param default: 未找到时间时的默认值
    :return: 处理后的时间值（0.3~10）
    """
    # 正则匹配所有数字+秒的组合（支持整数和小数）
    matches = re.findall(r'(\d+\.?\d*)\s*秒', text)
    
    if not matches:
        return default
    
    try:
        # 取最后一个时间参数（更符合自然语言习惯）
        duration = float(matches[-1])
    except ValueError:
        return default
    
    # 应用限制规则
    return min(max(duration, 0.3), 10)

def process_sentence(sentence, controller_state):
    # 增强版匹配规则（支持同义词组）
    isFound = False
    look_rules = {
        '左': {
            'patterns': [r'左看', r'看左', r'向左看', r'看左边'], 
            'func': r_left,
            'exclude': ['左']
        },
        '右': {
            'patterns': [r'右看', r'看右', r'向右看', r'看右边'],
            'func': r_right,
            'exclude': ['右']
        },
        '上': {
            'patterns': [r'上看', r'看上', r'向上看', r'看上方'],
            'func': r_up,
            'exclude': ['上']
        },
        '下': {
            'patterns': [r'下看', r'看下', r'向下看', r'看下面'],
            'func': r_down,
            'exclude': ['下']
        }
    }

    # 方向词映射
    direction_map = {
        '左': l_left,
        '右': l_right,
        '上': l_up,
        '下': l_down
    }

    triggered = set()
    found_directions = set()

    # 第一阶段：检测复合动作
    for direction, rule in look_rules.items():
        for pattern in rule['patterns']:
            if re.search(pattern, sentence):
                rule['func'](controller_state)
                isFound = True
                triggered.update(rule['exclude'])
                found_directions.add(direction)
                break  # 找到任意一个即触发

    # 第二阶段：检测基础方向词（带排除机制）
    for char, func in direction_map.items():
        # 检查是否包含方向字且未被排除
        if char in sentence and char not in triggered:
            # 排除已找到复合动作的情况
            if char not in found_directions:
                # 进一步验证是独立方向词（避免类似"左右"的情况）
                pattern = rf'(?:^|[\s，。！？]){char}(?=\d*秒?)|(?:^|[\s，。！？]){char}(?:$|[\s，。！？])'
                if re.search(pattern, sentence):
                    func(controller_state)
                    isFound = True
                    break  # 每个方向只触发一次
    return isFound


def receiverPipeAndSend(pipe_name, data_queue):
    print("hello ")
    try:
        pipe_fd = os.open(pipe_name, os.O_RDONLY | os.O_NONBLOCK)
        pipe = os.fdopen(pipe_fd)

        while True:
            ready, _, _ = select.select([pipe], [], [], 2.0)
            if pipe in ready:
                data = pipe.readline().strip()
                if data:
                    print(f"read pipe: {data}")
                    data_queue.put(data)
    except Exception as e:
        print(f"Error reading from pipe: {e}")
    finally:
        if pipe:
            pipe.close()
            print("close pipe")
        print("final block executed")
    print("end of pipe reading")


async def revice_from_pip(controller_state, data_queue):
    if controller_state.get_controller() != Controller.PRO_CONTROLLER:
        raise ValueError('This script only works with the Pro Controller!')

    # waits until controller is fully connected
    await controller_state.connect()
    await ainput(prompt='Make sure the Switch is in the Home menu and press <enter> to continue.')
    while True:
        if data_queue.empty():
            await asyncio.sleep(0.5)
        while not data_queue.empty():
            data = data_queue.get()
            if data:
                isFound = process_sentence(data, controller_state)
                needSecond =  extract_duration(data)
                input_str = []
                isButton = False
                for char in data:
                    if char in char_to_key:
                        input_str.append(char_to_key[char])
                        isButton = True
                if isFound or isButton:
                    
                    print(f"秒{needSecond} {input_str}")
                    await button_push(controller_state, *input_str, sec=needSecond)
                    if isFound:
                        controller_state.l_stick_state.set_center()
                        controller_state.r_stick_state.set_center()
                        await button_clear(controller_state)
                    




async def test_controller_buttons(controller_state: ControllerState):
    """
    Example controller script.
    Navigates to the "Test Controller Buttons" menu and presses all buttons.
    """
    if controller_state.get_controller() != Controller.PRO_CONTROLLER:
        raise ValueError('This script only works with the Pro Controller!')

    # waits until controller is fully connected
    await controller_state.connect()
#await ainput(prompt='Make sure the Switch is in the Home menu and press <enter> to continue.')
    """
    await ainput(prompt='Make sure the Switch is in the Home menu and press <enter> to continue.')

    # We assume we are in the "Change Grip/Order" menu of the switch

    # wait for the animation
    await asyncio.sleep(1)
    """

    """
    await button_push(controller_state, 'home', sec=1)
    await asyncio.sleep(1)
    """
    # Goto settings
    await asyncio.sleep(1)
    await button_push(controller_state, 'a')
    await asyncio.sleep(1)
    await button_push(controller_state, 'down', sec=1)
    await button_push(controller_state, 'right', sec=2)
    await asyncio.sleep(0.3)
    await button_push(controller_state, 'left')
    await asyncio.sleep(0.3)
    await button_push(controller_state, 'a')
    await asyncio.sleep(0.3)

    # go all the way down
    await button_push(controller_state, 'down', sec=4)
    await asyncio.sleep(0.3)

    # goto "Controllers and Sensors" menu
    for _ in range(2):
        await button_push(controller_state, 'up')
        await asyncio.sleep(0.3)
    await button_push(controller_state, 'right')
    await asyncio.sleep(0.3)

    # go all the way down
    await button_push(controller_state, 'down', sec=3)
    await asyncio.sleep(0.3)

    # goto "Test Input Devices" menu
    await button_push(controller_state, 'up')
    await asyncio.sleep(0.3)
    await button_push(controller_state, 'a')
    await asyncio.sleep(0.3)

    # goto "Test Controller Buttons" menu
    await button_push(controller_state, 'a')
    await asyncio.sleep(0.3)

    # push all buttons except home and capture
    button_list = controller_state.button_state.get_available_buttons()
    if 'capture' in button_list:
        button_list.remove('capture')
    if 'home' in button_list:
        button_list.remove('home')
    """
    button_list.remove('a')
# button_list.remove('b')
    button_list.remove('x')
    button_list.remove('y')
    button_list.remove('plus')
    button_list.remove('minus')
    """
    user_input = asyncio.ensure_future(
        ainput(prompt='Pressing all buttons... Press <enter> to stop.')
    )

    # push all buttons consecutively until user input
    while not user_input.done():
        for button in button_list:
            await button_push(controller_state, button)
            """
            stick = controller_state.l_stick_state
            # 将左摇杆向上推
            stick.set_h(128)  # 水平位置保持在中心
            stick.set_v(0)    # 垂直位置推到最上端

            # 将左摇杆向右推
            stick.set_h(255)  # 水平位置推到最右端
            stick.set_v(128)  # 垂直位置保持在中心

            # 将左摇杆归位到中心
            stick.set_h(128)
            stick.set_v(128)
            """
            await asyncio.sleep(0.1)
            print(f"button {button} \n")
            if user_input.done():
                break

    # await future to trigger exceptions in case something went wrong
    await user_input

    # go back to home
    await button_push(controller_state, 'home')


def ensure_valid_button(controller_state, *buttons):
    """
    Raise ValueError if any of the given buttons os not part of the controller state.
    :param controller_state:
    :param buttons: Any number of buttons to check (see ButtonState.get_available_buttons)
    """
    for button in buttons:
        if button not in controller_state.button_state.get_available_buttons():
            raise ValueError(f'Button {button} does not exist on {controller_state.get_controller()}')


async def mash_button(controller_state, button, interval):
    # wait until controller is fully connected
    await controller_state.connect()
    ensure_valid_button(controller_state, button)

    user_input = asyncio.ensure_future(
        ainput(prompt=f'Pressing the {button} button every {interval} seconds... Press <enter> to stop.')
    )
    # push a button repeatedly until user input
    while not user_input.done():
        await button_push(controller_state, button)
        await asyncio.sleep(float(interval))

    # await future to trigger exceptions in case something went wrong
    await user_input

def _register_commands_with_controller_state(controller_state, cli):
    """
    Commands registered here can use the given controller state.
    The doc string of commands will be printed by the CLI when calling "help"
    :param cli:
    :param controller_state:
    """
    async def test_buttons():
        """
        test_buttons - Navigates to the "Test Controller Buttons" menu and presses all buttons.
        """
        await test_controller_buttons(controller_state)

    cli.add_command(test_buttons.__name__, test_buttons)

    # Mash a button command
    async def mash(*args):
        """
        mash - Mash a specified button at a set interval

        Usage:
            mash <button> <interval>
        """
        if not len(args) == 2:
            raise ValueError('"mash_button" command requires a button and interval as arguments!')

        button, interval = args
        await mash_button(controller_state, button, interval)

    cli.add_command(mash.__name__, mash)

    async def click(*args):

        if not args:
            raise ValueError('"click" command requires a button!')

        await controller_state.connect()
        ensure_valid_button(controller_state, *args)

        await button_push(controller_state, *args)

    cli.add_command(click.__name__, click)

    # Hold a button command
    async def hold(*args):
        """
        hold - Press and hold specified buttons

        Usage:
            hold <button>

        Example:
            hold a b
        """
        if not args:
            raise ValueError('"hold" command requires a button!')

        ensure_valid_button(controller_state, *args)

        # wait until controller is fully connected
        await controller_state.connect()
        await button_press(controller_state, *args)

    cli.add_command(hold.__name__, hold)

    # Release a button command
    async def release(*args):
        """
        release - Release specified buttons

        Usage:
            release <button>

        Example:
            release a b
        """
        if not args:
            raise ValueError('"release" command requires a button!')

        ensure_valid_button(controller_state, *args)

        # wait until controller is fully connected
        await controller_state.connect()
        await button_release(controller_state, *args)

    cli.add_command(release.__name__, release)

    # Create nfc command
    async def nfc(*args):
        """
        nfc - Sets nfc content

        Usage:
            nfc <file_name>          Set controller state NFC content to file
            nfc remove               Remove NFC content from controller state
        """
        #logger.error('NFC Support was removed from joycontrol - see https://github.com/mart1nro/joycontrol/issues/80')
        if controller_state.get_controller() == Controller.JOYCON_L:
            raise ValueError('NFC content cannot be set for JOYCON_L')
        elif not args:
            raise ValueError('"nfc" command requires file path to an nfc dump or "remove" as argument!')
        elif args[0] == 'remove':
            controller_state.set_nfc(None)
            print('Removed nfc content.')
        else:
            controller_state.set_nfc(NFCTag.load_amiibo(args[0]))
            print("added nfc content")

    cli.add_command(nfc.__name__, nfc)

    async def pause(*args):
        """
        Pause regular input
        """
        controller_state._protocol.pause()

    cli.add_command(pause.__name__, pause)

    async def unpause(*args):
        """
        unpause regular input
        """
        controller_state._protocol.unpause()

    cli.add_command(unpause.__name__, unpause)

async def _main(args):
    # Get controller name to emulate from arguments
    controller = Controller.from_arg(args.controller)

    data_queue = queue.Queue()
    pipe_name = "/tmp/go_python_pipe"

    pipe_thread = threading.Thread(target=receiverPipeAndSend, args=(pipe_name, data_queue))
    pipe_thread.daemon = True
    pipe_thread.start()

    # parse the spi flash
    if args.spi_flash:
        with open(args.spi_flash, 'rb') as spi_flash_file:
            spi_flash = FlashMemory(spi_flash_file.read())
    else:
        # Create memory containing default controller stick calibration
        spi_flash = FlashMemory()


    with utils.get_output(path=args.log, default=None) as capture_file:
        # prepare the the emulated controller
        factory = controller_protocol_factory(controller, spi_flash=spi_flash, reconnect = args.reconnect_bt_addr)
        ctl_psm, itr_psm = 17, 19
        transport, protocol = await create_hid_server(factory, reconnect_bt_addr=args.reconnect_bt_addr,
                                                      ctl_psm=ctl_psm,
                                                      itr_psm=itr_psm, capture_file=capture_file,
                                                      device_id=args.device_id,
                                                      interactive=True)

        controller_state = protocol.get_controller_state()

        # Create command line interface and add some extra commands
        cli = ControllerCLI(controller_state)
        _register_commands_with_controller_state(controller_state, cli)
        cli.add_command('amiibo', ControllerCLI.deprecated('Command was removed - use "nfc" instead!'))
        cli.add_command(debug.debug.__name__, debug.debug)

        # set default nfc content supplied by argument
        if args.nfc is not None:
            await cli.commands['nfc'](args.nfc)

#await test_controller_buttons(controller_state)
        # run the cli
        await revice_from_pip(controller_state, data_queue)
        try:
            await cli.run()
        finally:
            logger.info('Stopping communication...')
            await transport.close()


if __name__ == '__main__':
    # check if root
    if not os.geteuid() == 0:
        raise PermissionError('Script must be run as root!')

    # setup logging
    #log.configure(console_level=logging.ERROR)
    log.configure()

    parser = argparse.ArgumentParser()
    parser.add_argument('controller', help='JOYCON_R, JOYCON_L or PRO_CONTROLLER')
    parser.add_argument('-l', '--log', help="BT-communication logfile output")
    parser.add_argument('-d', '--device_id', help='not fully working yet, the BT-adapter to use')
    parser.add_argument('--spi_flash', help="controller SPI-memory dump to use")
    parser.add_argument('-r', '--reconnect_bt_addr', type=str, default=None,
                        help='The Switch console Bluetooth address (or "auto" for automatic detection), for reconnecting as an already paired controller.')
    parser.add_argument('--nfc', type=str, default=None, help="amiibo dump placed on the controller. Äquivalent to the nfc command.")
    args = parser.parse_args()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        _main(args)
    )
