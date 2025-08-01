# Power Switch Control
#
# Copyright (C) 2024 Eric Callahan <arksine.code@gmail.com>
# Copyright (C) 2020 Jordan Ruthe <jordanruthe@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import logging
import struct
import socket
import asyncio
import time
import re
import shutil
from urllib.parse import quote, urlencode
from ..utils import json_wrapper as jsonw
from ..common import RequestType, KlippyState

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Type,
    List,
    Any,
    Optional,
    Dict,
    Coroutine,
    Union,
    cast
)

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .machine import Machine
    from .klippy_apis import KlippyAPI as APIComp
    from .mqtt import MQTTClient
    from .http_client import HttpClient
    from .klippy_connection import KlippyConnection
    from .shell_command import ShellCommandFactory as ShellCommand

class PrinterPower:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.devices: Dict[str, PowerDevice] = {}
        prefix_sections = config.get_prefix_sections("power")
        logging.info(f"Power component loading devices: {prefix_sections}")
        dev_types = {
            "gpio": GpioDevice,
            "klipper_device": KlipperDevice,
            "tplink_smartplug": TPLinkSmartPlug,
            "tasmota": Tasmota,
            "shelly": Shelly,
            "homeseer": HomeSeer,
            "homeassistant": HomeAssistant,
            "loxonev1": Loxonev1,
            "rf": RFDevice,
            "mqtt": MQTTDevice,
            "smartthings": SmartThings,
            "hue": HueDevice,
            "http": GenericHTTP,
            "uhubctl": UHubCtl
        }

        for section in prefix_sections:
            cfg = config[section]
            dev_type: str = cfg.get("type")
            dev_class: Optional[Type[PowerDevice]]
            dev_class = dev_types.get(dev_type)
            if dev_class is None:
                raise config.error(f"Unsupported Device Type: {dev_type}")
            try:
                dev = dev_class(cfg)
            except Exception as e:
                msg = f"Failed to load power device [{cfg.get_name()}]\n{e}"
                self.server.add_warning(msg, exc_info=e)
                continue
            self.devices[dev.get_name()] = dev

        self.server.register_endpoint(
            "/machine/device_power/devices", RequestType.GET, self._handle_list_devices
        )
        self.server.register_endpoint(
            "/machine/device_power/status", RequestType.GET,
            self._handle_batch_power_request
        )
        self.server.register_endpoint(
            "/machine/device_power/on", RequestType.POST,
            self._handle_batch_power_request
        )
        self.server.register_endpoint(
            "/machine/device_power/off", RequestType.POST,
            self._handle_batch_power_request
        )
        self.server.register_endpoint(
            "/machine/device_power/device", RequestType.GET | RequestType.POST,
            self._handle_single_power_request
        )
        self.server.register_remote_method(
            "set_device_power", self.set_device_power)
        self.server.register_event_handler(
            "server:klippy_shutdown", self._handle_klippy_shutdown)
        self.server.register_event_handler(
            "job_queue:job_queue_changed", self._handle_job_queued)
        self.server.register_notification("power:power_changed")

    async def component_init(self) -> None:
        for dev in self.devices.values():
            if not dev.initialize():
                self.server.add_warning(
                    f"Power device '{dev.get_name()}' failed to initialize"
                )

    def _handle_klippy_shutdown(self) -> None:
        for dev in self.devices.values():
            dev.process_klippy_shutdown()

    async def _handle_job_queued(self, queue_info: Dict[str, Any]) -> None:
        if queue_info["action"] != "jobs_added":
            return
        for name, dev in self.devices.items():
            if dev.should_turn_on_when_queued():
                queue: List[Dict[str, Any]]
                queue = queue_info.get("updated_queue", [])
                fname = "unknown"
                if len(queue):
                    fname = queue[0].get("filename", "unknown")
                logging.info(
                    f"Power Device {name}: Job '{fname}' queued, powering on"
                )
                await dev.process_request("on")

    async def _handle_list_devices(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        dev_list = [d.get_device_info() for d in self.devices.values()]
        output = {"devices": dev_list}
        return output

    async def _handle_single_power_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        dev_name: str = web_request.get_str('device')
        req_type = web_request.get_request_type()
        if dev_name not in self.devices:
            raise self.server.error(f"No valid device named {dev_name}")
        dev = self.devices[dev_name]
        if req_type == RequestType.GET:
            action = "status"
        elif req_type == RequestType.POST:
            action = web_request.get_str('action').lower()
            if action not in ["on", "off", "toggle"]:
                raise self.server.error(f"Invalid requested action '{action}'")
        else:
            raise self.server.error(f"Invalid Request Type: {req_type}")
        result = await dev.process_request(action)
        return {dev_name: result}

    async def _handle_batch_power_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        args = web_request.get_args()
        ep = web_request.get_endpoint()
        if not args:
            raise self.server.error("No arguments provided")
        requested_devs = {k: self.devices.get(k, None) for k in args}
        result = {}
        req = ep.split("/")[-1]
        for name, device in requested_devs.items():
            if device is not None:
                result[name] = await device.process_request(req)
            else:
                result[name] = "device_not_found"
        return result

    def set_device_power(
        self, device: str, state: Union[bool, str], force: bool = False
    ) -> None:
        request: str = ""
        if isinstance(state, bool):
            request = "on" if state else "off"
        elif isinstance(state, str):
            request = state.lower()
            if request in ["true", "false"]:
                request = "on" if request == "true" else "off"
        if request not in ["on", "off", "toggle"]:
            logging.info(f"Invalid state received: {state}")
            return
        if device not in self.devices:
            logging.info(f"No device found: {device}")
            return
        event_loop = self.server.get_event_loop()
        event_loop.register_callback(
            self.devices[device].process_request, request, force=force
        )

    async def add_device(self, name: str, device: PowerDevice) -> None:
        if name in self.devices:
            raise self.server.error(
                f"Device [{name}] already configured")
        success = device.initialize()
        if asyncio.iscoroutine(success):
            success = await success  # type: ignore
        if not success:
            self.server.add_warning(
                f"Power device '{device.get_name()}' failed to initialize"
            )
            return
        self.devices[name] = device

    async def close(self) -> None:
        coros: List[Coroutine] = []
        for device in self.devices.values():
            ret = device.close()
            if ret is not None:
                coros.append(ret)
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

class PowerDevice:
    def __init__(self, config: ConfigHelper, can_poll: bool = True) -> None:
        name_parts = config.get_name().split(maxsplit=1)
        if len(name_parts) != 2:
            raise config.error(f"Invalid Section Name: {config.get_name()}")
        self.server = config.get_server()
        self.name = name_parts[1]
        self.type: str = config.get('type')
        self.state: str = "init"
        self.request_lock = asyncio.Lock()
        self.init_task: Optional[asyncio.Task] = None
        self.locked_while_printing = config.getboolean(
            'locked_while_printing', False)
        self.off_when_shutdown = config.getboolean('off_when_shutdown', False)
        self.off_when_shutdown_delay = 0.
        if self.off_when_shutdown:
            self.off_when_shutdown_delay = config.getfloat(
                'off_when_shutdown_delay', 0., minval=0.)
        self.shutdown_timer_handle: Optional[asyncio.TimerHandle] = None
        self.restart_delay = 1.
        self.klipper_restart = config.getboolean(
            'restart_klipper_when_powered', False)
        if self.klipper_restart:
            self.restart_delay = config.getfloat(
                'restart_delay', 1., above=.000001
            )
            self.server.register_event_handler(
                "server:klippy_started", self._schedule_firmware_restart
            )
        self.bound_services: List[str] = []
        bound_services: List[str] = config.getlist('bound_services', [])
        if config.has_option('bound_service'):
            # The `bound_service` option is deprecated, however this minimal
            # change does not require a warning as it can be reliably resolved
            bound_services.append(config.get('bound_service'))
        for svc in bound_services:
            if svc.endswith(".service"):
                svc = svc.rsplit(".", 1)[0]
            if svc in self.bound_services:
                continue
            self.bound_services.append(svc)
        self.need_scheduled_restart = False
        self.on_when_queued = config.getboolean('on_when_job_queued', False)
        if config.has_option('on_when_upload_queued'):
            self.on_when_queued = config.getboolean('on_when_upload_queued',
                                                    False, deprecate=True)
        self.initial_state: Optional[bool] = config.getboolean(
            'initial_state', None
        )
        self.restrict_actions = config.getboolean("restrict_action_processing", True)
        self._poll_task: asyncio.Task | None = None
        self._poll_interval = None
        if can_poll:
            self._poll_interval = config.getfloat("poll_interval", None, minval=1.0)
        self._last_update_time = 0.

    def _schedule_firmware_restart(self, state: KlippyState) -> None:
        if not self.need_scheduled_restart:
            return
        self.need_scheduled_restart = False
        if state == KlippyState.READY:
            logging.info(
                f"Power Device {self.name}: Klipper reports 'ready', "
                "aborting FIRMWARE_RESTART"
            )
            return
        logging.info(
            f"Power Device {self.name}: Sending FIRMWARE_RESTART command "
            "to Klippy"
        )
        event_loop = self.server.get_event_loop()
        kapis: APIComp = self.server.lookup_component("klippy_apis")
        event_loop.delay_callback(
            self.restart_delay, kapis.do_restart,
            "FIRMWARE_RESTART", True
        )

    def get_name(self) -> str:
        return self.name

    def get_device_info(self) -> Dict[str, Any]:
        return {
            'device': self.name,
            'status': self.state,
            'locked_while_printing': self.locked_while_printing,
            'type': self.type
        }

    def notify_power_changed(self) -> None:
        dev_info = self.get_device_info()
        self.server.send_event("power:power_changed", dev_info)

    async def process_power_changed(self) -> None:
        self.notify_power_changed()
        if self.bound_services:
            await self.process_bound_services()
        if self.state == "on" and self.klipper_restart:
            self.need_scheduled_restart = True
            kconn: KlippyConnection = self.server.lookup_component("klippy_connection")
            klippy_state = kconn.state
            if not klippy_state.startup_complete():
                # If klippy is currently disconnected or hasn't proceeded past
                # the startup state, schedule the restart in the
                # "klippy_started" event callback.
                return
            self._schedule_firmware_restart(klippy_state)

    async def process_bound_services(self) -> None:
        if not self.bound_services or self.state not in ("on", "off"):
            return
        machine_cmp: Machine = self.server.lookup_component("machine")
        action = "start" if self.state == "on" else "stop"
        for svc in self.bound_services:
            logging.info(
                f"Power Device {self.name}: Performing {action} action "
                f"on bound service {svc}"
            )
            try:
                await machine_cmp.do_service_action(action, svc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception(
                    f"Power Device {self.name}: Error processing {action} action "
                    f"for bound service {svc}"
                )

    def process_klippy_shutdown(self) -> None:
        if not self.off_when_shutdown:
            return
        if self.off_when_shutdown_delay == 0.:
            self._power_off_on_shutdown()
        else:
            if self.shutdown_timer_handle is not None:
                self.shutdown_timer_handle.cancel()
                self.shutdown_timer_handle = None
            event_loop = self.server.get_event_loop()
            self.shutdown_timer_handle = event_loop.delay_callback(
                self.off_when_shutdown_delay, self._power_off_on_shutdown)

    def _power_off_on_shutdown(self) -> None:
        kconn: KlippyConnection = self.server.lookup_component("klippy_connection")
        if kconn.state != KlippyState.SHUTDOWN:
            return
        logging.info(
            f"Powering off device '{self.name}' due to klippy shutdown")
        power: PrinterPower = self.server.lookup_component("power")
        power.set_device_power(self.name, "off")

    def should_turn_on_when_queued(self) -> bool:
        return self.on_when_queued

    def _setup_bound_services(self) -> None:
        if not self.bound_services:
            return
        machine_cmp: Machine = self.server.lookup_component("machine")
        sys_info = machine_cmp.get_system_info()
        avail_svcs: List[str] = sys_info.get('available_services', [])
        for svc in self.bound_services:
            if machine_cmp.unit_name == svc:
                raise self.server.error(
                    f"Power Device {self.name}: Cannot bind to Moonraker "
                    f"service {svc}."
                )
            if svc not in avail_svcs:
                raise self.server.error(
                    f"Bound Service {svc} is not available"
                )
        svcs = ", ".join(self.bound_services)
        logging.info(f"Power Device '{self.name}' bound to services: {svcs}")

    def init_state(self) -> Optional[Coroutine]:
        return None

    def initialize(self) -> bool:
        self._setup_bound_services()
        ret = self.init_state()
        if ret is not None:
            eventloop = self.server.get_event_loop()
            self.init_task = eventloop.create_task(ret)
        return self.state != "error"

    async def _set_initial_state(self) -> None:
        if self.initial_state is not None and self.state in ("on", "off"):
            new_state = "on" if self.initial_state else "off"
            try:
                if new_state != self.state:
                    logging.info(
                        f"Power Device {self.name}: setting "
                        f"initial state to {new_state}"
                    )
                    ret = self.set_power(new_state)
                    if ret is not None:
                        await ret
                if self.bound_services:
                    await self.process_bound_services()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.info(f"Power Device {self.name}: Error setting initial state")

    async def process_request(self, req: str, force: bool = False) -> str:
        if self.state == "init" and self.request_lock.locked():
            # return immediately if the device is initializing,
            # otherwise its possible for this to block indefinitely
            # while the device holds the lock
            return self.state
        async with self.request_lock:
            try:
                base_state: str = self.state
                ret = self.refresh_status()
                if ret is not None:
                    await ret
                cur_state: str = self.state
                if req == "toggle":
                    req = "on" if cur_state == "off" else "off"
                if req in ["on", "off"]:
                    if req == cur_state:
                        # device is already in requested state, do nothing
                        if base_state != cur_state:
                            if self.restrict_actions:
                                self.notify_power_changed()
                            else:
                                await self.process_power_changed()
                        return cur_state
                    if not force:
                        kconn: KlippyConnection
                        kconn = self.server.lookup_component("klippy_connection")
                        if self.locked_while_printing and kconn.is_printing():
                            raise self.server.error(
                                f"Unable to change power for {self.name} "
                                "while printing")
                    ret = self.set_power(req)
                    if ret is not None:
                        await ret
                    cur_state = self.state
                    await self.process_power_changed()
                elif req != "status":
                    raise self.server.error(f"Unsupported power request: {req}")
                elif base_state != cur_state:
                    if self.restrict_actions:
                        self.notify_power_changed()
                    else:
                        await self.process_power_changed()
            finally:
                eventloop = self.server.get_event_loop()
                self._last_update_time = eventloop.get_loop_time()
            return cur_state

    def refresh_status(self) -> Optional[Coroutine]:
        raise NotImplementedError

    def set_power(self, state: str) -> Optional[Coroutine]:
        raise NotImplementedError

    async def _notify_external_change(self) -> None:
        async with self.request_lock:
            if self.restrict_actions:
                self.notify_power_changed()
            else:
                await self.process_power_changed()
            eventloop = self.server.get_event_loop()
            self._last_update_time = eventloop.get_loop_time()

    async def _poll_device(self) -> None:
        eventloop = self.server.get_event_loop()
        while self._poll_interval is not None:
            time_diff = eventloop.get_loop_time() - self._last_update_time
            if time_diff >= self._poll_interval:
                try:
                    await self.process_request("status")
                except asyncio.CancelledError:
                    break
                except Exception:
                    pass
                time_diff = self._poll_interval or 0.
            await asyncio.sleep(time_diff)

    def _should_log_error(self) -> bool:
        # Avoid logging refresh errors when polling devices
        # unless verbose logging is enabled
        if self.server.is_verbose_enabled():
            return True
        return asyncio.current_task() is not self._poll_task

    def start_polling(self) -> bool:
        if self._poll_interval is None or self._poll_task is not None:
            return False
        eventloop = self.server.get_event_loop()
        self._poll_task = eventloop.create_task(self._poll_device())
        return True

    def stop_polling(self) -> Optional[Coroutine]:
        if self._poll_task is None:
            return None
        poll_task = self._poll_task
        self._poll_task = None
        # If not sleeping, attempt to abort after status request is complete
        if self.request_lock.locked():
            self._poll_interval = None
            return asyncio.wait_for(poll_task, 1.)
        poll_task.cancel()
        return None

    def close(self) -> Optional[Coroutine]:
        if self.init_task is not None:
            self.init_task.cancel()
            self.init_task = None
        return self.stop_polling()

class HTTPDevice(PowerDevice):
    def __init__(
        self,
        config: ConfigHelper,
        default_port: int = -1,
        default_user: str = "",
        default_password: str = "",
        default_protocol: str = "http",
        is_generic: bool = False
    ) -> None:
        super().__init__(config)
        self.client: HttpClient = self.server.lookup_component("http_client")
        self.user = config.load_template("user", default_user).render()
        self.password = config.load_template("password", default_password).render()
        self.has_basic_auth: bool = False
        if is_generic:
            return
        addr_parts = config.get("address").strip("/").split("/")
        self.addr: str = "/".join([quote(part) for part in addr_parts])
        self.port = config.getint("port", default_port)
        self.protocol = config.get("protocol", default_protocol)
        if self.port == -1:
            self.port = 443 if self.protocol.lower() == "https" else 80

    def enable_basic_authentication(self) -> None:
        if self.user and self.password:
            self.has_basic_auth = True

    async def init_state(self) -> None:
        async with self.request_lock:
            last_err: Exception = Exception()
            while True:
                try:
                    state = await self._send_status_request()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if type(last_err) is not type(e) or last_err.args != e.args:
                        logging.exception(f"Device Init Error: {self.name}")
                        last_err = e
                    await asyncio.sleep(5.)
                    continue
                else:
                    self.init_task = None
                    self.state = state
                    await self._set_initial_state()
                    self.notify_power_changed()
                    eventloop = self.server.get_event_loop()
                    self._last_update_time = eventloop.get_loop_time()
                    self.start_polling()
                    return

    async def _send_http_command(
        self, url: str, command: str, retries: int = 3
    ) -> Dict[str, Any]:
        ba_user: Optional[str] = None
        ba_pass: Optional[str] = None
        if self.has_basic_auth:
            ba_user = self.user
            ba_pass = self.password
        response = await self.client.get(
            url, request_timeout=20., attempts=retries, retry_pause_time=1.,
            enable_cache=False, basic_auth_user=ba_user, basic_auth_pass=ba_pass
        )
        response.raise_for_status(
            f"Error sending '{self.type}' command: {command}")
        data = cast(dict, response.json())
        return data

    async def _send_power_request(self, state: str) -> str:
        raise NotImplementedError(
            "_send_power_request must be implemented by children")

    async def _send_status_request(self) -> str:
        raise NotImplementedError(
            "_send_status_request must be implemented by children")

    async def refresh_status(self) -> None:
        try:
            state = await self._send_status_request()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state = "error"
            if self._should_log_error():
                logging.exception(f"Error Refreshing Device Status: {self.name}")
        else:
            self.state = state

    async def set_power(self, state):
        try:
            state = await self._send_power_request(state)
        except Exception:
            self.state = "error"
            msg = f"Error Setting Device Status: {self.name} to {state}"
            logging.exception(msg)
            raise self.server.error(msg) from None
        self.state = state


class GpioDevice(PowerDevice):
    def __init__(self,
                 config: ConfigHelper,
                 initial_val: Optional[int] = None
                 ) -> None:
        super().__init__(config, False)
        self.timer: Optional[float] = config.getfloat('timer', None)
        if self.timer is not None and self.timer < 0.000001:
            raise config.error(
                f"Option 'timer' in section [{config.get_name()}] must "
                "be above 0.0")
        self.timer_handle: Optional[asyncio.TimerHandle] = None
        if initial_val is None:
            initial_val = int(self.initial_state or 0)
        self.gpio_out = config.getgpioout('pin', initial_value=initial_val)

    async def init_state(self) -> None:
        async with self.request_lock:
            try:
                if self.initial_state is None:
                    self.set_power("off")
                else:
                    self.set_power("on" if self.initial_state else "off")
                    if self.bound_services:
                        await self.process_bound_services()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception(f"Power Device {self.name}: Initialization failed")
            finally:
                eventloop = self.server.get_event_loop()
                self._last_update_time = eventloop.get_loop_time()

    def refresh_status(self) -> None:
        pass

    def set_power(self, state) -> None:
        if self.timer_handle is not None:
            self.timer_handle.cancel()
            self.timer_handle = None
        try:
            self.gpio_out.write(int(state == "on"))
        except Exception:
            self.state = "error"
            msg = f"Error Toggling Device Power: {self.name}"
            logging.exception(msg)
            raise self.server.error(msg) from None
        self.state = state
        self._check_timer()

    def _check_timer(self) -> None:
        if self.state == "on" and self.timer is not None:
            event_loop = self.server.get_event_loop()
            power: PrinterPower = self.server.lookup_component("power")
            self.timer_handle = event_loop.delay_callback(
                self.timer, power.set_device_power, self.name, "off")

    def close(self) -> None:
        if self.timer_handle is not None:
            self.timer_handle.cancel()
            self.timer_handle = None

class KlipperDevice(PowerDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, False)
        if self.off_when_shutdown:
            raise config.error(
                "Option 'off_when_shutdown' in section "
                f"[{config.get_name()}] is unsupported for 'klipper_device'")
        if self.klipper_restart:
            raise config.error(
                "Option 'restart_klipper_when_powered' in section "
                f"[{config.get_name()}] is unsupported for 'klipper_device'")
        self.is_shutdown: bool = False
        self.update_fut: Optional[asyncio.Future] = None
        self.timer: Optional[float] = config.getfloat(
            'timer', None, above=0.000001)
        self.timer_handle: Optional[asyncio.TimerHandle] = None
        self.object_name = config.get('object_name')
        obj_parts = self.object_name.split()
        self.gc_cmd = f"SET_PIN PIN={obj_parts[-1]}"
        if obj_parts[0] == "gcode_macro":
            self.gc_cmd = obj_parts[-1]
        elif obj_parts[0] != "output_pin":
            raise config.error(
                "Klipper object must be either 'output_pin' or 'gcode_macro' "
                f"for option 'object_name' in section [{config.get_name()}]")

        self.server.register_event_handler(
            "server:klippy_ready", self._handle_ready)
        self.server.register_event_handler(
            "server:klippy_disconnect", self._handle_disconnect)

    def _status_update(self, data: Dict[str, Any], _: float) -> None:
        self._set_state_from_data(data)

    def get_device_info(self) -> Dict[str, Any]:
        dev_info = super().get_device_info()
        dev_info['is_shutdown'] = self.is_shutdown
        return dev_info

    async def _handle_ready(self) -> None:
        kconn: KlippyConnection = self.server.lookup_component("klippy_connection")
        if kconn.unit_name:
            if kconn.unit_name in self.bound_services:
                self.server.add_warning(
                    f"Power: Klipper Device {self.name} contains invalid "
                    f"bound service {kconn.unit_name}.  Remove this service "
                    "from the configuration and restart Moonraker.",
                    f"kpd_{self.name}"
                )
                self.bound_services.remove(kconn.unit_name)
        kapis: APIComp = self.server.lookup_component('klippy_apis')
        sub: Dict[str, Optional[List[str]]] = {self.object_name: None}
        data = await kapis.subscribe_objects(sub, self._status_update, None)
        if not self._validate_data(data):
            self.state = "error"
        else:
            async with self.request_lock:
                assert data is not None
                self._set_state_from_data(data)
                await self._set_initial_state()
                self.notify_power_changed()

    async def _handle_disconnect(self) -> None:
        self.is_shutdown = False
        self._set_state("init")
        self._reset_timer()

    def process_klippy_shutdown(self) -> None:
        self.is_shutdown = True
        self._set_state("error")
        self._reset_timer()

    async def refresh_status(self) -> None:
        if self.is_shutdown or self.state in ["on", "off", "init"]:
            return
        kapis: APIComp = self.server.lookup_component('klippy_apis')
        req: Dict[str, Optional[List[str]]] = {self.object_name: None}
        data: Optional[Dict[str, Any]]
        data = await kapis.query_objects(req, None)
        if not self._validate_data(data):
            self.state = "error"
        else:
            assert data is not None
            self._set_state_from_data(data)

    async def set_power(self, state: str) -> None:
        if self.is_shutdown:
            raise self.server.error(
                f"Power Device {self.name}: Cannot set power for device "
                f"when Klipper is shutdown")
        self._reset_timer()
        eventloop = self.server.get_event_loop()
        self.update_fut = eventloop.create_future()
        try:
            kapis: APIComp = self.server.lookup_component('klippy_apis')
            value = "1" if state == "on" else "0"
            await kapis.run_gcode(f"{self.gc_cmd} VALUE={value}")
            assert self.update_fut is not None
            await asyncio.wait_for(self.update_fut, 1.)
        except TimeoutError:
            self.state = "error"
            raise self.server.error(
                f"Power device {self.name}: Timeout "
                "waiting for device state update")
        except Exception:
            self.state = "error"
            msg = f"Error Toggling Device Power: {self.name}"
            logging.exception(msg)
            raise self.server.error(msg) from None
        finally:
            self.update_fut = None
        self._check_timer()

    def _validate_data(self, data: Optional[Dict[str, Any]]) -> bool:
        if data is None:
            logging.info("Error querying klipper object: "
                         f"{self.object_name}")
        elif self.object_name not in data:
            logging.info(
                f"[power]: Invalid Klipper Device {self.object_name}, "
                f"no response returned from subscription.")
        elif 'value' not in data[self.object_name]:
            logging.info(
                f"[power]: Invalid Klipper Device {self.object_name}, "
                f"response does not contain a 'value' parameter")
        else:
            return True
        return False

    def _set_state_from_data(self, data: Dict[str, Any]) -> None:
        if self.object_name not in data:
            return
        value = data[self.object_name].get('value')
        if value is not None:
            state = "on" if value else "off"
            self._set_state(state)
            if self.update_fut is not None:
                self.update_fut.set_result(state)

    def _set_state(self, state: str) -> None:
        in_request = self.request_lock.locked()
        last_state = self.state
        self.state = state
        eventloop = self.server.get_event_loop()
        self._last_update_time = eventloop.get_loop_time()
        if last_state not in [state, "init"] and not in_request:
            eventloop.create_task(self._notify_external_change())

    def _check_timer(self) -> None:
        if self.state == "on" and self.timer is not None:
            event_loop = self.server.get_event_loop()
            power: PrinterPower = self.server.lookup_component("power")
            self.timer_handle = event_loop.delay_callback(
                self.timer, power.set_device_power, self.name, "off")

    def _reset_timer(self) -> None:
        if self.timer_handle is not None:
            self.timer_handle.cancel()
            self.timer_handle = None

    def close(self) -> None:
        if self.timer_handle is not None:
            self.timer_handle.cancel()
            self.timer_handle = None

class RFDevice(GpioDevice):

    # Protocol definition
    # [1, 3] means HIGH is set for 1x pulse_len and LOW for 3x pulse_len
    ZERO_BIT = [1, 3]  # zero bit
    ONE_BIT = [3, 1]  # one bit
    SYNC_BIT = [1, 31]  # sync between
    PULSE_LEN = 0.00035  # length of a single pulse
    RETRIES = 10  # send the code this many times

    def __init__(self, config: ConfigHelper):
        super().__init__(config, initial_val=0)
        self.on = config.get("on_code").zfill(24)
        self.off = config.get("off_code").zfill(24)

    def _transmit_digit(self, waveform) -> None:
        self.gpio_out.write(1)
        time.sleep(waveform[0]*RFDevice.PULSE_LEN)
        self.gpio_out.write(0)
        time.sleep(waveform[1]*RFDevice.PULSE_LEN)

    def _transmit_code(self, code) -> None:
        for _ in range(RFDevice.RETRIES):
            for i in code:
                if i == "1":
                    self._transmit_digit(RFDevice.ONE_BIT)
                elif i == "0":
                    self._transmit_digit(RFDevice.ZERO_BIT)
            self._transmit_digit(RFDevice.SYNC_BIT)

    def set_power(self, state) -> None:
        try:
            if state == "on":
                code = self.on
            else:
                code = self.off
            self._transmit_code(code)
        except Exception:
            self.state = "error"
            msg = f"Error Toggling Device Power: {self.name}"
            logging.exception(msg)
            raise self.server.error(msg) from None
        self.state = state
        self._check_timer()


#  This implementation based off the work tplink_smartplug
#  script by Lubomir Stroetmann available at:
#
#  https://github.com/softScheck/tplink-smartplug
#
#  Copyright 2016 softScheck GmbH
class TPLinkSmartPlug(PowerDevice):
    START_KEY = 0xAB
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config)
        self.timer = config.get("timer", "")
        addr_and_output_id = config.get("address").split('/')
        self.addr = addr_and_output_id[0]
        if (len(addr_and_output_id) > 1):
            self.server.add_warning(
                f"Power Device {self.name}: Including the output id in the"
                " address is deprecated, use the 'output_id' option")
            self.output_id: Optional[int] = int(addr_and_output_id[1])
        else:
            self.output_id = config.getint("output_id", None)
        self.port = config.getint("port", 9999)

    async def _send_tplink_command(self,
                                   command: str
                                   ) -> Dict[str, Any]:
        out_cmd: Dict[str, Any] = {}
        if command in ["on", "off"]:
            out_cmd = {
                'system': {'set_relay_state': {'state': int(command == "on")}}
            }
            # TPLink device controls multiple devices
            if self.output_id is not None:
                sysinfo = await self._send_tplink_command("info")
                children = sysinfo["system"]["get_sysinfo"]["children"]
                child_id = children[self.output_id]["id"]
                out_cmd["context"] = {"child_ids": [f"{child_id}"]}
        elif command == "info":
            out_cmd = {'system': {'get_sysinfo': {}}}
        elif command == "clear_rules":
            out_cmd = {'count_down': {'delete_all_rules': None}}
        elif command == "count_off":
            out_cmd = {
                'count_down': {'add_rule':
                               {'enable': 1, 'delay': int(self.timer),
                                'act': 0, 'name': 'turn off'}}
            }
        else:
            raise self.server.error(f"Invalid tplink command: {command}")
        reader, writer = await asyncio.open_connection(
            self.addr, self.port, family=socket.AF_INET)
        try:
            writer.write(self._encrypt(out_cmd))
            await writer.drain()
            data = await reader.read(2048)
            length: int = struct.unpack(">I", data[:4])[0]
            data = data[4:]
            retries = 5
            remaining = length - len(data)
            while remaining and retries:
                data += await reader.read(remaining)
                remaining = length - len(data)
                retries -= 1
            if not retries:
                raise self.server.error("Unable to read tplink packet")
        except Exception:
            msg = f"Error sending tplink command: {command}"
            logging.exception(msg)
            raise self.server.error(msg)
        finally:
            writer.close()
            await writer.wait_closed()
        return jsonw.loads(self._decrypt(data))

    def _encrypt(self, outdata: Dict[str, Any]) -> bytes:
        data = jsonw.dumps(outdata)
        key = self.START_KEY
        res = struct.pack(">I", len(data))
        for c in data:
            val = key ^ c
            key = val
            res += bytes([val])
        return res

    def _decrypt(self, data: bytes) -> str:
        key: int = self.START_KEY
        res: str = ""
        for c in data:
            val = key ^ c
            key = c
            res += chr(val)
        return res

    async def _send_info_request(self) -> int:
        res = await self._send_tplink_command("info")
        if self.output_id is not None:
            # TPLink device controls multiple devices
            children: Dict[int, Any]
            children = res['system']['get_sysinfo']['children']
            return children[self.output_id]['state']
        else:
            return res['system']['get_sysinfo']['relay_state']

    async def init_state(self) -> None:
        async with self.request_lock:
            last_err: Exception = Exception()
            while True:
                try:
                    state: int = await self._send_info_request()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if type(last_err) is not type(e) or last_err.args != e.args:
                        logging.exception(f"Device Init Error: {self.name}")
                        last_err = e
                    await asyncio.sleep(5.)
                    continue
                else:
                    self.init_task = None
                    self.state = "on" if state else "off"
                    await self._set_initial_state()
                    self.notify_power_changed()
                    eventloop = self.server.get_event_loop()
                    self._last_update_time = eventloop.get_loop_time()
                    self.start_polling()
                    return

    async def refresh_status(self) -> None:
        try:
            state: int = await self._send_info_request()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state = "error"
            if self._should_log_error():
                logging.exception(f"Error Refreshing Device Status: {self.name}")
        else:
            self.state = "on" if state else "off"

    async def set_power(self, state) -> None:
        err: int
        try:
            if self.timer != "" and state == "off":
                await self._send_tplink_command("clear_rules")
                res = await self._send_tplink_command("count_off")
                err = res['count_down']['add_rule']['err_code']
            else:
                res = await self._send_tplink_command(state)
                err = res['system']['set_relay_state']['err_code']
        except Exception:
            err = 1
            logging.exception(f"Power Toggle Error: {self.name}")
        if err:
            self.state = "error"
            raise self.server.error(
                f"Error Toggling Device Power: {self.name}")
        self.state = state


class Tasmota(HTTPDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, default_user="admin", default_password="")
        self.output_id = config.getint("output_id", 1)
        self.timer = config.get("timer", "")

    async def _send_tasmota_command(self, command: str) -> Dict[str, Any]:
        if command in ["on", "off"]:
            out_cmd = f"Power{self.output_id} {command}"
            if self.timer != "" and command == "off":
                out_cmd = f"Backlog Delay {self.timer}0; {out_cmd}"
        elif command == "info":
            out_cmd = f"Power{self.output_id}"
        else:
            raise self.server.error(f"Invalid tasmota command: {command}")
        query = urlencode({
            "user": self.user,
            "password": self.password,
            "cmnd": out_cmd
        })
        url = f"{self.protocol}://{self.addr}/cm?{query}"
        return await self._send_http_command(url, command)

    async def _send_status_request(self) -> str:
        res = await self._send_tasmota_command("info")
        try:
            state: str = res[f"POWER{self.output_id}"].lower()
        except KeyError as e:
            if self.output_id == 1:
                state = res["POWER"].lower()
            else:
                raise KeyError(e)
        return state

    async def _send_power_request(self, state: str) -> str:
        res = await self._send_tasmota_command(state)
        if self.timer == "" or state != "off":
            try:
                state = res[f"POWER{self.output_id}"].lower()
            except KeyError as e:
                if self.output_id == 1:
                    state = res["POWER"].lower()
                else:
                    raise KeyError(e)
        return state


class Shelly(HTTPDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, default_user="admin", default_password="")
        self.output_id = config.getint("output_id", 0)
        self.timer = config.get("timer", "")
        self.enable_basic_authentication()

    async def _send_shelly_command(self, command: str) -> Dict[str, Any]:
        query_args: Dict[str, Any] = {}
        out_cmd = f"relay/{self.output_id}"
        if command in ["on", "off"]:
            query_args["turn"] = command
            if command == "off" and self.timer != "":
                query_args["turn"] = "on"
                query_args["timer"] = self.timer
        elif command != "info":
            raise self.server.error(f"Invalid shelly command: {command}")
        query = urlencode(query_args)
        url = f"{self.protocol}://{self.addr}/{out_cmd}?{query}"
        return await self._send_http_command(url, command)

    async def _send_status_request(self) -> str:
        res = await self._send_shelly_command("info")
        state: str = res["ison"]
        timer_remaining = res["timer_remaining"] if self.timer != "" else 0
        return "on" if state and timer_remaining == 0 else "off"

    async def _send_power_request(self, state: str) -> str:
        res = await self._send_shelly_command(state)
        state = res["ison"]
        timer_remaining = res["timer_remaining"] if self.timer != "" else 0
        return "on" if state and timer_remaining == 0 else "off"


class SmartThings(HTTPDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, default_port=443, default_protocol="https")
        self.device: str = config.get("device", "")
        self.token: str = config.gettemplate("token").render()

    async def _send_smartthings_command(self, command: str) -> Dict[str, Any]:
        body: Optional[List[Dict[str, Any]]] = None
        if (command == "on" or command == "off"):
            method = "POST"
            url = (
                f"{self.protocol}://{self.addr}"
                f"/v1/devices/{quote(self.device)}/commands"
            )
            body = [
                {
                    "component": "main",
                    "capability": "switch",
                    "command": command
                }
            ]
        elif command == "info":
            method = "GET"
            url = (
                f"{self.protocol}://{self.addr}/v1/devices/"
                f"{quote(self.device)}/components/main/capabilities/"
                "switch/status"
            )
        else:
            raise self.server.error(
                f"Invalid SmartThings command: {command}")

        headers = {
            'Authorization': f'Bearer {self.token}'
        }
        response = await self.client.request(
            method, url, body=body, headers=headers,
            attempts=3, enable_cache=False
        )
        msg = f"Error sending SmartThings command: {command}"
        response.raise_for_status(msg)
        data = cast(dict, response.json())
        return data

    async def _send_status_request(self) -> str:
        res = await self._send_smartthings_command("info")
        return res["switch"]["value"].lower()

    async def _send_power_request(self, state: str) -> str:
        res = await self._send_smartthings_command(state)
        acknowledgment = res["results"][0]["status"].lower()
        return state if acknowledgment == "accepted" else "error"


class HomeSeer(HTTPDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, default_user="admin", default_password="")
        self.device = config.getint("device")
        self.enable_basic_authentication()

    async def _send_homeseer(
        self, request: str, state: str = ""
    ) -> Dict[str, Any]:
        query_args = {
            "user": self.user,
            "pass": self.password,
            "request": request,
            "ref": self.device,
        }
        if state:
            query_args["label"] = state
        query = urlencode(query_args)
        url = (
            f"{self.protocol}://{self.addr}:{self.port}/JSON?{query}"
        )
        return await self._send_http_command(url, request)

    async def _send_status_request(self) -> str:
        res = await self._send_homeseer("getstatus")
        return res["Devices"][0]["status"].lower()

    async def _send_power_request(self, state: str) -> str:
        await self._send_homeseer(
            "controldevicebylabel", state.capitalize()
        )
        return state


class HomeAssistant(HTTPDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, default_port=8123)
        self.device: str = config.get("device")
        self.token: str = config.gettemplate("token").render()
        self.domain: str = config.get("domain", "switch")
        self.status_delay: float = config.getfloat("status_delay", 1.)

    async def _send_homeassistant_command(self, command: str) -> Dict[str, Any]:
        body: Optional[Dict[str, Any]] = None
        if command in ["on", "off"]:
            out_cmd = f"api/services/{quote(self.domain)}/turn_{command}"
            body = {"entity_id": self.device}
            method = "POST"
        elif command == "info":
            out_cmd = f"api/states/{quote(self.device)}"
            method = "GET"
        else:
            raise self.server.error(
                f"Invalid homeassistant command: {command}")
        url = f"{self.protocol}://{self.addr}:{self.port}/{out_cmd}"
        headers = {
            'Authorization': f'Bearer {self.token}'
        }
        data: Dict[str, Any] = {}
        response = await self.client.request(
            method, url, body=body, headers=headers,
            attempts=3, enable_cache=False
        )
        msg = f"Error sending homeassistant command: {command}"
        response.raise_for_status(msg)
        if method == "GET":
            data = cast(dict, response.json())
        return data

    async def _send_status_request(self) -> str:
        res = await self._send_homeassistant_command("info")
        return res["state"]

    async def _send_power_request(self, state: str) -> str:
        await self._send_homeassistant_command(state)
        await asyncio.sleep(self.status_delay)
        res = await self._send_status_request()
        return res

class Loxonev1(HTTPDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, default_user="admin",
                         default_password="admin")
        self.output_id = config.get("output_id", "")
        self.enable_basic_authentication()

    async def _send_loxonev1_command(self, command: str) -> Dict[str, Any]:
        if command in ["on", "off"]:
            out_cmd = f"jdev/sps/io/{quote(self.output_id)}/{command}"
        elif command == "info":
            out_cmd = f"jdev/sps/io/{quote(self.output_id)}"
        else:
            raise self.server.error(f"Invalid loxonev1 command: {command}")
        url = f"http://{self.addr}/{out_cmd}"
        return await self._send_http_command(url, command)

    async def _send_status_request(self) -> str:
        res = await self._send_loxonev1_command("info")
        state = res["LL"]["value"]
        return "on" if int(state) == 1 else "off"

    async def _send_power_request(self, state: str) -> str:
        res = await self._send_loxonev1_command(state)
        state = res["LL"]["value"]
        return "on" if int(state) == 1 else "off"


class MQTTDevice(PowerDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, False)
        self.mqtt: MQTTClient = self.server.load_component(config, 'mqtt')
        self.eventloop = self.server.get_event_loop()
        self.cmd_topic: str = config.get('command_topic')
        self.cmd_payload = config.gettemplate('command_payload')
        self.retain_cmd_state = config.getboolean('retain_command_state', False)
        self.query_topic: Optional[str] = config.get('query_topic', None)
        self.query_payload = config.gettemplate('query_payload', None)
        self.must_query = config.getboolean('query_after_command', False)
        if self.query_topic is None:
            self.must_query = False
        self.last_request: str = ""
        self.response_count: int = 0

        self.state_topic: str = config.get('state_topic')
        self.state_timeout = config.getfloat('state_timeout', 2.)
        self.state_response = config.load_template('state_response_template',
                                                   "{payload}")
        self.qos: Optional[int] = config.getint('qos', None, minval=0, maxval=2)
        self.mqtt.subscribe_topic(
            self.state_topic, self._on_state_update, self.qos)
        self.query_response: Optional[asyncio.Future] = None
        self.server.register_event_handler(
            "mqtt:connected", self._on_mqtt_connected)
        self.server.register_event_handler(
            "mqtt:disconnected", self._on_mqtt_disconnected)

    def _on_state_update(self, payload: bytes) -> None:
        last_state = self.state
        in_request = self.request_lock.locked()
        self.response_count += 1
        err: Optional[Exception] = None
        context = {
            "payload": payload.decode(),
            "last_request": self.last_request,
            "response_count": self.response_count
        }
        response: str = ""
        try:
            response = self.state_response.render(context)
        except Exception as e:
            err = e
            self.state = "error"
        else:
            response = response.lower()
            if response == "discard":
                return
            elif response not in ["on", "off"]:
                err_msg = "Invalid State Received. " \
                    f"Raw Payload: '{payload.decode()}', Rendered: '{response}"
                logging.info(f"MQTT Power Device {self.name}: {err_msg}")
                err = self.server.error(err_msg, 500)
                self.state = "error"
            else:
                self.state = response
        self._last_update_time = self.eventloop.get_loop_time()
        if not in_request and last_state != self.state:
            logging.info(
                f"MQTT Power Device {self.name}: External Power "
                f"event detected, new state: {self.state}"
            )
            self.eventloop.create_task(self._notify_external_change())
        if (
            self.query_response is not None and
            not self.query_response.done()
        ):
            if err is not None:
                self.query_response.set_exception(err)
            else:
                self.query_response.set_result(response)

    async def _on_mqtt_connected(self) -> None:
        async with self.request_lock:
            if self.state in ["on", "off"]:
                return
            self.state = "init"
            success = False
            while self.mqtt.is_connected():
                self.query_response = self.eventloop.create_future()
                try:
                    assert self.query_response is not None
                    await self._wait_for_update(self.query_response)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    # Only wait once if no query topic is set.
                    # Assume that the MQTT device has set the retain
                    # flag on the state topic, and therefore should get
                    # an immediate response upon subscription.
                    if self.query_topic is None:
                        logging.info(f"MQTT Power Device {self.name}: "
                                     "Initialization Timed Out")
                        break
                except Exception:
                    logging.exception(f"MQTT Power Device {self.name}: Init Failed")
                    break
                else:
                    success = True
                    break
                await asyncio.sleep(2.)
            self.query_response = None
            if not success:
                self.state = "error"
            else:
                logging.info(f"MQTT Power Device {self.name} initialized")
            await self._set_initial_state()
            # Don't reset on next connection
            self.initial_state = None
            self.notify_power_changed()

    async def _on_mqtt_disconnected(self):
        if (
            self.query_response is not None and
            not self.query_response.done()
        ):
            self.query_response.set_exception(
                self.server.error("MQTT Disconnected", 503))
        async with self.request_lock:
            self.state = "error"
            self.notify_power_changed()

    async def refresh_status(self) -> None:
        if (
            self.query_topic is not None and
            (self.must_query or self.state not in ["on", "off"])
        ):
            if not self.mqtt.is_connected():
                raise self.server.error(
                    f"MQTT Power Device {self.name}: MQTT Not Connected", 503
                )
            self.last_request = "refresh"
            self.response_count = 0
            self.query_response = self.eventloop.create_future()
            try:
                assert self.query_response is not None
                await self._wait_for_update(self.query_response)
            except Exception:
                logging.exception(
                    f"MQTT Power Device {self.name}: Failed to refresh state"
                )
                self.state = "error"
            self.query_response = None
            self.last_request = ""
            self.response_count = 0

    async def _wait_for_update(self, fut: asyncio.Future,
                               do_query: bool = True
                               ) -> str:
        if self.query_topic is not None and do_query:
            payload: Optional[str] = None
            if self.query_payload is not None:
                payload = self.query_payload.render()
            await self.mqtt.publish_topic(self.query_topic, payload,
                                          self.qos)
        return await asyncio.wait_for(fut, timeout=self.state_timeout)

    async def set_power(self, state: str) -> None:
        if not self.mqtt.is_connected():
            raise self.server.error(
                f"MQTT Power Device {self.name}: "
                "MQTT Not Connected", 503)
        self.query_response = self.eventloop.create_future()
        self.last_request = f"power_{state}"
        self.response_count = 0
        new_state = "error"
        try:
            assert self.query_response is not None
            payload = self.cmd_payload.render({'command': state})
            await self.mqtt.publish_topic(
                self.cmd_topic, payload, self.qos,
                retain=self.retain_cmd_state)
            new_state = await self._wait_for_update(
                self.query_response, do_query=self.must_query)
        except Exception:
            logging.exception(
                f"MQTT Power Device {self.name}: Failed to set state")
            new_state = "error"
        self.query_response = None
        self.last_request = ""
        self.response_count = 0
        self.state = new_state
        if self.state == "error":
            raise self.server.error(
                f"MQTT Power Device {self.name}: Failed to set "
                f"device to state '{state}'", 500)


class HueDevice(HTTPDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, default_port=80)
        self.device_id = config.get("device_id")
        self.device_type = config.get("device_type", "light")
        if self.device_type == "group":
            self.state_key = "action"
            self.on_state = "all_on"
        else:
            self.state_key = "state"
            self.on_state = "on"

    async def _send_power_request(self, state: str) -> str:
        new_state = True if state == "on" else False
        url = (
            f"{self.protocol}://{self.addr}:{self.port}/api/{quote(self.user)}"
            f"/{self.device_type}s/{quote(self.device_id)}"
            f"/{quote(self.state_key)}"
        )
        ret = await self.client.request("PUT", url, body={"on": new_state})
        resp = cast(List[Dict[str, Dict[str, Any]]], ret.json())
        state_url = (
            f"/{self.device_type}s/{self.device_id}/{self.state_key}/on"
        )
        return (
            "on" if resp[0]["success"][state_url]
            else "off"
        )

    async def _send_status_request(self) -> str:
        url = (
            f"{self.protocol}://{self.addr}:{self.port}/api/{quote(self.user)}"
            f"/{self.device_type}s/{quote(self.device_id)}"
        )
        ret = await self.client.request("GET", url)
        resp = cast(Dict[str, Dict[str, Any]], ret.json())
        return "on" if resp["state"][self.on_state] else "off"

class GenericHTTP(HTTPDevice):
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config, is_generic=True)
        self.urls: Dict[str, str] = {
            "on": config.gettemplate("on_url").render(),
            "off": config.gettemplate("off_url").render(),
            "status": config.gettemplate("status_url").render()
        }
        self.request_template = config.gettemplate(
            "request_template", None, is_async=True
        )
        self.response_template = config.gettemplate("response_template", is_async=True)
        self.enable_basic_authentication()

    async def _send_generic_request(self, command: str) -> str:
        ba_user: Optional[str] = None
        ba_pass: Optional[str] = None
        if self.has_basic_auth:
            ba_user = self.user
            ba_pass = self.password
        request = self.client.wrap_request(
            self.urls[command], request_timeout=20., attempts=3, retry_pause_time=1.,
            basic_auth_user=ba_user, basic_auth_pass=ba_pass
        )
        context: Dict[str, Any] = {
            "command": command,
            "http_request": request,
            "async_sleep": asyncio.sleep,
            "urls": dict(self.urls)
        }
        if self.request_template is not None:
            await self.request_template.render_async(context)
            response = request.last_response()
            if response is None:
                raise self.server.error("Failed to receive a response")
        else:
            response = await request.send()
        response.raise_for_status()
        result = (await self.response_template.render_async(context)).lower()
        if result not in ["on", "off"]:
            raise self.server.error(f"Invalid result: {result}")
        return result

    async def _send_power_request(self, state: str) -> str:
        return await self._send_generic_request(state)

    async def _send_status_request(self) -> str:
        return await self._send_generic_request("status")


HUB_STATE_PATTERN = r"""
    (?:Port\s(?P<port>[0-9]+):)
    (?:\s(?P<bits>[0-9a-f]{4}))
    (?:\s(?P<pstate>power|off))
    (?P<flags>(?:\s[0-9a-z.]+)+)?
    (?:\s\[(?P<desc>.+)\])?
"""

class UHubCtl(PowerDevice):
    _uhubctrl_regex = re.compile(
        r"^\s*" + HUB_STATE_PATTERN + r"\s*$",
        re.VERBOSE | re.IGNORECASE
    )
    def __init__(self, config: ConfigHelper) -> None:
        super().__init__(config)
        self.scmd: ShellCommand = self.server.load_component(config, "shell_command")
        self.location = config.get("location")
        self.port = config.getint("port", None)
        ret = shutil.which("uhubctl")
        if ret is None:
            raise config.error(
                f"[{config.get_name()}]: failed to locate 'uhubctl' binary.  "
                "Make sure uhubctl is correctly installed on the host machine."
            )

    async def init_state(self) -> None:
        async with self.request_lock:
            await self.refresh_status(is_init=True)
            await self._set_initial_state()
            eventloop = self.server.get_event_loop()
            self._last_update_time = eventloop.get_loop_time()
            self.start_polling()

    async def refresh_status(self, is_init: bool = False) -> None:
        try:
            result = await self._run_uhubctl("info")
        except self.server.error as e:
            self.state = "error"
            if not self._should_log_error():
                return
            output = f"\n{e}"
            if isinstance(e, self.scmd.error):
                output += f"\nuhubctrl output: {e.stderr.decode(errors='ignore')}"
            ename = "Refresh" if is_init else "Initialization"
            logging.info(f"Power Device {self.name}: {ename} Error{output}")
        else:
            if asyncio.current_task() is not self._poll_task:
                logging.debug(
                    f"Power Device {self.name}: uhubctl device info: {result}"
                )
            self.state = result["state"]

    async def set_power(self, state: str) -> None:
        try:
            result = await self._run_uhubctl(state)
        except self.server.error as e:
            self.state = "error"
            msg = f"Power Device {self.name}: Error turning device {state}"
            output = f"\n{e}"
            if isinstance(e, self.scmd.error):
                output += f"\nuhubctrl output: {e.stderr.decode(errors='ignore')}"
            logging.info(f"{msg}{output}")
            raise self.server.error(msg) from None
        logging.debug(f"Power Device {self.name}: uhubctl device info: {result}")
        self.state = result["state"]

    async def _run_uhubctl(self, action: str) -> Dict[str, Any]:
        cmd = f"uhubctl -l {self.location}"
        if self.port is not None:
            cmd += f" -p {self.port}"
        search_prefix = f"Current status for hub {self.location}"
        if action in ["on", "off"]:
            cmd += f" -a {action}"
            search_prefix = f"New status for hub {self.location}"
        resp: str = await self.scmd.exec_cmd(cmd, log_complete=False)
        for line in resp.splitlines():
            if search_prefix:
                if line.startswith(search_prefix):
                    search_prefix = ""
                continue
            match = self._uhubctrl_regex.match(line.strip())
            if match is None:
                continue
            result = match.groupdict()
            try:
                port = int(result["port"])
                status_bits = int(result["bits"], 16)
            except (TypeError, ValueError):
                continue
            if self.port is not None and port != self.port:
                continue
            if result["pstate"] is None:
                continue
            state = "on" if result["pstate"] == "power" else "off"
            flags: List[str] = []
            if result["flags"] is not None:
                flags = result["flags"].strip().split()
            return {
                "port": port,
                "status_bits": status_bits,
                "state": state,
                "flags": flags,
                "desc": result["desc"]
            }
        raise self.server.error(
            f"Failed to receive response for device at location {self.location}, "
            f"port {self.port}, "
        )


# The power component has multiple configuration sections
def load_component(config: ConfigHelper) -> PrinterPower:
    return PrinterPower(config)
