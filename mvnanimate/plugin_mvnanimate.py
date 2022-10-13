# Copyright Epic Games, Inc. All Rights Reserved.

from collections import deque
import datetime
import select
import socket
import struct
from threading import Thread
import time
from xml.etree.ElementTree import Element, tostring, fromstring, ParseError

from switchboard.config import IntSetting, DirectoryPathSetting
from switchboard.devices.device_base import Device, DeviceStatus
from switchboard.devices.device_widget_base import DeviceWidget
from switchboard.switchboard_logging import LOGGER
import switchboard.switchboard_utils as utils


class DeviceMVNAnimate(Device):
    setting_mvnanimate_port = IntSetting(
        "mvnanimate_port", "MVN Animate UDP Remote Port", 6004)

    setting_session_path = DirectoryPathSetting(
        "session_path", "Current session recording path", "")

    def __init__(self, name, ip_address, **kwargs):
        super().__init__(name, ip_address, **kwargs)

        self.trigger_start = True
        self.trigger_stop = True

        self.client = None

        self._slate = 'slate'
        self._take = 1

        # Stores pairs of (queued message, command name).
        self.message_queue = deque()

        self.socket = None
        self.close_socket = False
        self.last_activity = datetime.datetime.now()
        self.awaiting_echo_response = False
        self.command_response_callbacks = {
            "IdentifyAck": self.on_mvn_identify_response,
            "StartRecordingAck": self.on_mvn_recording_started,
            "StopRecordingAck": self.on_mvn_recording_stopped,
            "CaptureNameAck": self.on_mvn_record_take_name_set
        }
        # self.command_response_callbacks = {
        #     "Connect": self.on_mvn_connect_response,
        #     "Echo": self.on_mvn_identify_response,
        #     "SetRecordTakeName": self.on_mvn_record_take_name_set,
        #     "StartRecording": self.on_mvn_recording_started,
        #     "StopRecording": self.on_mvn_recording_stopped}
        self.mvn_connection_thread = None

    @staticmethod
    def plugin_settings():
        return Device.plugin_settings() + [DeviceMVNAnimate.setting_mvnanimate_port, DeviceMVNAnimate.setting_session_path]

    def send_request_to_mvn(self, message_type, data):
        """ Sends a request message to MVN's command port. """

        elem = Element(message_type)
        for key, val in data.items():
            elem.set(key, val)
        
        message_str = tostring(elem)
        LOGGER.warning(f"XML string : {str(data)}")
       
        self.send_string_to_mvn(message_str)

    def send_string_to_mvn(self, data):
        """ Sends a raw string to MVN's command port. """
        self.message_queue.appendleft(data)

    def send_identify_request(self):
        """
        Sends an identify request to MVN if not already waiting for an identify
        response.
        """
        if self.awaiting_echo_response:
            return

        self.awaiting_echo_response = True
        self.send_request_to_mvn("IdentifyReq", {})
        

    def on_mvn_identify_response(self, response):
        """
        Callback that is executed when MVN has responded to an echo request.
        """

        self.awaiting_echo_response = False
        self.status = DeviceStatus.READY
        
        LOGGER.warning("MVN identify response: {}".format(tostring(response)))


    @property
    def is_connected(self):
        return self.socket is not None

    def connect_listener(self):
        """ Connect to MVN's socket """
        self.close_socket = False

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # UDP
        self.socket.bind(('', 0))

        self.last_activity = datetime.datetime.now()

        self.mvn_connection_thread = Thread(target=self.mvn_connection)
        self.mvn_connection_thread.start()

        self.awaiting_echo_response = False
        self.send_identify_request()

    def disconnect_listener(self):
        """ Disconnect from MVN """
        if self.is_connected:
            self.close_socket = True
            self.mvn_connection_thread.join()

            # updates device state
            super().disconnect_listener()

    def mvn_connection(self):
        """
        Thread procedure for socket connection between Switchboard and MVN.
        """
        ping_interval = 1.0
        disconnect_timeout = 3.0

        while self.is_connected:
            try:
                rlist = [self.socket]
                wlist = []
                xlist = []
                read_timeout = 0.2

                if len(self.message_queue):
                    message_str = self.message_queue.pop()

                    self._flush_read_sockets()
                    self.socket.sendto(
                        message_str,
                        (self.ip_address,
                         self.setting_mvnanimate_port.get_value()))

                    read_sockets, _, _ = select.select(
                        rlist, wlist, xlist, read_timeout)
                    for rs in read_sockets:
                        received_data = rs.recv(4096)
                        self.process_message(received_data)
                else:
                    time.sleep(0.01)

                activity_delta = datetime.datetime.now() - self.last_activity

                if activity_delta.total_seconds() > disconnect_timeout:
                    raise Exception("Connection timeout")
                elif activity_delta.total_seconds() > ping_interval:
                    self.send_identify_request()

                if self.close_socket and len(self.message_queue) == 0:
                    self.socket.shutdown(socket.SHUT_RDWR)
                    self.socket.close()
                    self.socket = None
                    break

            except Exception as e:
                LOGGER.warning(f"{self.name}: Disconnecting due to: {e}")
                self.device_qt_handler.signal_device_client_disconnected.emit(
                    self)
                break

    def _flush_read_sockets(self):
        """ Flushes all sockets used for receiving data. """
        rlist = [self.socket]
        wlist = []
        xlist = []
        read_timeout = 0
        read_sockets, _, _ = select.select(rlist, wlist, xlist, read_timeout)
        for rs in read_sockets:
            orig_timeout = rs.gettimeout()
            rs.settimeout(0)
            data = b'foo'
            try:
                while len(data) > 0:
                    data = rs.recv(32768)
            except socket.error:
                pass
            finally:
                rs.settimeout(orig_timeout)

    def process_message(self, data):
        """ Processes incoming messages sent by MVN. """

        self.last_activity = datetime.datetime.now()
        try:
            msg_elem = fromstring(data)
        except ParseError as e:
            LOGGER.error("Could not parse MVN message {}".format(data))
            return

        if msg_elem.tag in self.command_response_callbacks:
            self.command_response_callbacks[msg_elem.tag](msg_elem)
        else:
            LOGGER.error(
                f"{self.name}: Could not find callback for "
                f"{cmd_name} request")

    def set_take(self, take_name, take_num):
        """ Notify MVN when Take number was changed. """
        self._slate = take_name
        self._take = take_num

        msg_elem = Element("CaptureName")
        
        slate_elem = Element("Name")
        state_elem.set("VALUE", value)
        msg_elem.append(slate_elem)

        take_elem = Element("Take")
        take_elem.set("VALUE", value)
        msg_elem.append(take_elem)
        
        LOGGER.warning("MVN set take to {}".format(msg_elem))
        self.send_string_to_mvn(tostring(msg_elem), {})

    def on_mvn_record_take_name_set(self, response):
        """ Callback that is executed when the take name was set in MVN. """
        LOGGER.warning(f"MVN set take to {str(tostring(response))}")
        pass

    def record_start(self, slate, take, description):
        """
        Called by switchboard_dialog when recording was started, will start
        recording in MVN.
        """
        LOGGER.warning("Starting MVN recording")

        if self.is_disconnected or not self.trigger_start:
            return

        #self.set_take(slate, take)
        
        recording_req_data = {
            "SessionName": slate,
            "Description": description
        }
        LOGGER.warning(f"MVN recording data {str(recording_req_data)}")
        self.send_request_to_mvn('StartRecordingReq', recording_req_data)

    def on_mvn_recording_started(self, response):
        """ Callback that is exectued when MVN has started recording. """
        self.record_start_confirm(self.timecode())

    def record_stop(self):
        """
        Called by switchboard_dialog when recording was stopped, will stop
        recording in MVN.
        """
        LOGGER.warning("Stopping MVN recording")
        if self.is_disconnected or not self.trigger_stop:
            return

        self.send_request_to_mvn('StopRecordingReq', {})

    def on_mvn_recording_stopped(self, response):
        """ Callback that is exectued when MVN has stopped recording. """
        self.record_stop_confirm(self.timecode(), paths=None)

    def timecode(self):
        return '00:00:00:00'


class DeviceWidgetMVNAnimate(DeviceWidget):
    def __init__(self, name, device_hash, ip_address, icons, parent=None):
        super().__init__(name, device_hash, ip_address, icons, parent=parent)

    def _add_control_buttons(self):
        super()._add_control_buttons()
        self.trigger_start_button = self.add_control_button(
            ':/icons/images/icon_trigger_start_disabled.png',
            icon_hover=':/icons/images/icon_trigger_start_hover.png',
            icon_disabled=':/icons/images/icon_trigger_start_disabled.png',
            icon_on=':/icons/images/icon_trigger_start.png',
            icon_hover_on=':/icons/images/icon_trigger_start_hover.png',
            icon_disabled_on=':/icons/images/icon_trigger_start_disabled.png',
            tool_tip='Trigger when recording starts',
            checkable=True, checked=True)

        self.trigger_stop_button = self.add_control_button(
            ':/icons/images/icon_trigger_stop_disabled.png',
            icon_hover=':/icons/images/icon_trigger_stop_hover.png',
            icon_disabled=':/icons/images/icon_trigger_stop_disabled.png',
            icon_on=':/icons/images/icon_trigger_stop.png',
            icon_hover_on=':/icons/images/icon_trigger_stop_hover.png',
            icon_disabled_on=':/icons/images/icon_trigger_stop_disabled.png',
            tool_tip='Trigger when recording stops',
            checkable=True, checked=True)

        self.connect_button = self.add_control_button(
            ':/icons/images/icon_connect.png',
            icon_hover=':/icons/images/icon_connect_hover.png',
            icon_disabled=':/icons/images/icon_connect_disabled.png',
            icon_on=':/icons/images/icon_connected.png',
            icon_hover_on=':/icons/images/icon_connected_hover.png',
            icon_disabled_on=':/icons/images/icon_connected_disabled.png',
            tool_tip='Connect/Disconnect from listener')

        self.trigger_start_button.clicked.connect(self.trigger_start_clicked)
        self.trigger_stop_button.clicked.connect(self.trigger_stop_clicked)
        self.connect_button.clicked.connect(self.connect_button_clicked)

        # Disable the buttons
        self.trigger_start_button.setDisabled(True)
        self.trigger_stop_button.setDisabled(True)

    def trigger_start_clicked(self):
        if self.trigger_start_button.isChecked():
            self.signal_device_widget_trigger_start_toggled.emit(self, True)
        else:
            self.signal_device_widget_trigger_start_toggled.emit(self, False)

    def trigger_stop_clicked(self):
        if self.trigger_stop_button.isChecked():
            self.signal_device_widget_trigger_stop_toggled.emit(self, True)
        else:
            self.signal_device_widget_trigger_stop_toggled.emit(self, False)

    def connect_button_clicked(self):
        if self.connect_button.isChecked():
            self._connect()
        else:
            self._disconnect()

    def _connect(self):
        # Make sure the button is in the correct state
        self.connect_button.setChecked(True)

        # Enable the buttons
        self.trigger_start_button.setDisabled(False)
        self.trigger_stop_button.setDisabled(False)

        # Emit Signal to Switchboard
        self.signal_device_widget_connect.emit(self)

    def _disconnect(self):
        # Make sure the button is in the correct state
        self.connect_button.setChecked(False)

        # Disable the buttons
        self.trigger_start_button.setDisabled(True)
        self.trigger_stop_button.setDisabled(True)

        # Emit Signal to Switchboard
        self.signal_device_widget_disconnect.emit(self)
