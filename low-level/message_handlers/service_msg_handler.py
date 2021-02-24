# Copyright (c) 2001-2020 Seagate Technology LLC and/or its Affiliates
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>. For any questions
# about this software or licensing, please email opensource@seagate.com or
# cortx-questions@seagate.com.

"""
 ****************************************************************************
  Description:       Message Handler for service request messages
 ****************************************************************************
"""

import errno
import json
import socket
import time
import uuid

from framework.actuator_state_manager import actuator_state_manager
from framework.base.module_thread import ScheduledModuleThread
from framework.base.internal_msgQ import InternalMsgQ
from framework.utils.service_logging import logger
from framework.base.sspl_constants import enabled_products
from json_msgs.messages.actuators.service_controller import ServiceControllerMsg
from json_msgs.messages.sensors.service_watchdog import ServiceWatchdogMsg
from json_msgs.messages.actuators.ack_response import AckResponseMsg
# Modules that receive messages from this module
from message_handlers.logging_msg_handler import LoggingMsgHandler
# from cortx.utils.conf_store import Conf
from framework.utils.conf_utils import (
    CLUSTER, CLUSTER_ID, GLOBAL_CONF, NODE_ID, RACK_ID, SITE_ID, SRVNODE, Conf)
from framework.utils.severity_reader import SeverityReader


class ServiceMsgHandler(ScheduledModuleThread, InternalMsgQ):
    """Message Handler for service request messages"""

    MODULE_NAME = "ServiceMsgHandler"
    PRIORITY = 2

    # Dependency list
    DEPENDENCIES = {
        "plugins": [
            "LoggingMsgHandler",
            "RabbitMQegressProcessor"],
        "rpms": []
    }

    @staticmethod
    def dependencies():
        """Returns a list of plugins and RPMs this module requires
           to function.
        """
        return ServiceMsgHandler.DEPENDENCIES

    @staticmethod
    def name():
        """ @return: name of the module."""
        return ServiceMsgHandler.MODULE_NAME

    def __init__(self):
        super(ServiceMsgHandler, self).__init__(self.MODULE_NAME,
                                                self.PRIORITY)
        self._service_actuator = None
        self._query_utility = None

        # Flag to indicate suspension of module
        self._suspended = False

    def initialize(self, conf_reader, msgQlist, product):
        """initialize configuration reader and internal msg queues"""
        # Initialize ScheduledMonitorThread
        super(ServiceMsgHandler, self).initialize(conf_reader)

        # Initialize internal message queues for this module
        super(ServiceMsgHandler, self).initialize_msgQ(msgQlist)

        self._import_products(product)

    def _import_products(self, product):
        """Import classes based on which product is being used"""
        if product.lower() in [x.lower() for x in enabled_products]:
            from zope.component import queryUtility
            self._query_utility = queryUtility

    def run(self):
        """Run the module periodically on its own thread."""
        self._log_debug("Start accepting requests")

        # Do not proceed if module is suspended
        if self._suspended == True:
            self._scheduler.enter(1, self._priority, self.run, ())
            return

        # self._set_debug(True)
        # self._set_debug_persist(True)

        try:
            # Block on message queue until it contains an entry
            json_msg, _ = self._read_my_msgQ()
            if json_msg is not None:
                self._process_msg(json_msg)

            # Keep processing until the message queue is empty
            while not self._is_my_msgQ_empty():
                json_msg, _ = self._read_my_msgQ()
                if json_msg is not None:
                    self._process_msg(json_msg)

        except Exception as ae:
            # Log it and restart the whole process when a failure occurs
            logger.exception(f"ServiceMsgHandler restarting: {ae}")

        self._scheduler.enter(1, self._priority, self.run, ())
        self._log_debug("Finished processing successfully")

    def _process_msg(self, jsonMsg):
        """Parses the incoming message and hands off to the appropriate logger
        """
        self._log_debug(f"_process_msg, jsonMsg: {jsonMsg}")

        if isinstance(jsonMsg, dict) is False:
            jsonMsg = json.loads(jsonMsg)

        # Parse out the uuid so that it can be sent back in Ack message
        uuid = None
        if jsonMsg.get("sspl_ll_msg_header") is not None and \
           jsonMsg.get("sspl_ll_msg_header").get("uuid") is not None:
            uuid = jsonMsg.get("sspl_ll_msg_header").get("uuid")
            self._log_debug(f"_processMsg, uuid: {uuid}")

        # Handle service start, stop, restart, status requests
        if jsonMsg.get("actuator_request_type").get("service_controller") is not None:
            self._log_debug("_processMsg, msg_type: service_controller")

            service_name = jsonMsg.get("actuator_request_type") \
                .get("service_controller").get("service_name")
            service_request = jsonMsg.get("actuator_request_type") \
                .get("service_controller").get("service_request")
            request = f"{service_request}:{service_name}"

            # If the state is INITIALIZED, We can assume that actuator is
            # ready to perform operation.
            if actuator_state_manager.is_initialized("Service"):
                self._log_debug(f"_process_msg, service_actuator name: {self._service_actuator.name()}")
                self._execute_request(self._service_actuator, jsonMsg, uuid)

            # If the state is INITIALIZING, need to send message
            elif actuator_state_manager.is_initializing("Service"):
                # This state will not be reached. Kept here for consistency.
                logger.info("Service actuator is initializing")
                busy_json_msg = AckResponseMsg(
                    request, "BUSY", uuid, error_no=errno.EBUSY).getJson()
                self._write_internal_msgQ(
                    "RabbitMQegressProcessor", busy_json_msg)

            elif actuator_state_manager.is_imported("Service"):
                # This case will be for first request only. Subsequent
                # requests will go to INITIALIZED state case.
                logger.info("Service actuator is imported and initializing")
                from actuators.IService import IService
                actuator_state_manager.set_state(
                    "Service", actuator_state_manager.INITIALIZING)
                service_actuator_class = self._query_utility(IService)
                if service_actuator_class:
                    # NOTE: Instantiation part should not time consuming
                    # otherwise ServiceMsgHandler will get block and will
                    # not be able serve any subsequent requests. This applies
                    # to instantiation of evey actuator.
                    self._service_actuator = service_actuator_class()
                    logger.info(f"_process_msg, service_actuator name: {self._service_actuator.name()}")
                    self._execute_request(
                        self._service_actuator, jsonMsg, uuid)
                    actuator_state_manager.set_state(
                        "Service", actuator_state_manager.INITIALIZED)
                else:
                    logger.info("Service actuator is not instantiated")

            # If there is no entry for actuator in table, We can assume
            # that it is not loaded for some reason.
            else:
                logger.warn("Service actuator is not loaded or not supported")

        # Handle events generated by the service watchdogs
        elif jsonMsg.get("actuator_request_type").get("service_watchdog_controller") is not None:
            self._log_debug("_processMsg, msg_type: service_watchdog_controller")

            # Parse out values to be sent
            service_name = jsonMsg.get("actuator_request_type").get("service_watchdog_controller").get("service_name")
            state = jsonMsg.get("actuator_request_type").get("service_watchdog_controller").get("state")
            prev_state = jsonMsg.get("actuator_request_type").get("service_watchdog_controller").get("previous_state")
            substate = jsonMsg.get("actuator_request_type").get("service_watchdog_controller").get("substate")
            prev_substate = jsonMsg.get("actuator_request_type").get("service_watchdog_controller").get("previous_substate")
            pid = jsonMsg.get("actuator_request_type").get("service_watchdog_controller").get("pid")
            prev_pid = jsonMsg.get("actuator_request_type").get("service_watchdog_controller").get("previous_pid")

            # Pull out the service_request and if it's equal to "status" then get current status (state, substate)
            service_request = jsonMsg.get("actuator_request_type").get("service_watchdog_controller").get("service_request")
            if service_request != "None":
                # Query the Zope GlobalSiteManager for an object implementing IService
                if self._service_actuator is None:
                    from actuators.IService import IService
                    self._service_actuator = self._query_utility(IService)()
                    self._log_debug(f"_process_msg, service_actuator name: {self._service_actuator.name()}")
                service_name, state, substate = self._service_actuator.perform_request(jsonMsg)

                self._log_debug(f"_processMsg, service_name: {service_name}, state: {state}, substate: {substate}")
                self._log_debug(f"_processMsg, prev state: {prev_state}, prev substate: {prev_substate}")

            _internal_json_msg = self._gen_internal_msg(jsonMsg)
            # Create a service watchdog message and send it out
            jsonMsg = ServiceWatchdogMsg(_internal_json_msg).getJson()
            self._write_internal_msgQ("RabbitMQegressProcessor", jsonMsg)

            # Create an IEM if the resulting service state is failed
            if "fail" in state.lower() or \
                "fail" in substate.lower():
                json_data = {"service_name": service_name,
                             "state": state,
                             "previous_state": prev_state,
                             "substate": substate,
                             "previous_substate": prev_substate,
                             "pid": pid,
                             "previous_pid": prev_pid
                            }

                internal_json_msg = json.dumps(
                    {"actuator_request_type" : {
                        "logging": {
                            "log_level": "LOG_WARNING",
                            "log_type": "IEM",
                            "log_msg": f"IEC: 020003001: Service entered a Failed state : {json.dumps(json_data, sort_keys=True)}"
                            }
                        }
                    })

                # Send the event to logging msg handler to send IEM message to journald
                self._write_internal_msgQ(
                    LoggingMsgHandler.name(), internal_json_msg)

        # ... handle other service message types

    def _gen_internal_msg(self, jsonMsg):
        """ Generate json message"""

        alert_type = "fault"
        severity_reader = SeverityReader()
        severity = severity_reader.map_severity(alert_type)

        epoch_time = str(int(time.time()))

        alert_id = self._get_alert_id(epoch_time)
        host_name = socket.gethostname()

        self._site_id = Conf.get(GLOBAL_CONF, f"{CLUSTER}>{SRVNODE}>{SITE_ID}",'DC01')
        self._rack_id = Conf.get(GLOBAL_CONF, f"{CLUSTER}>{SRVNODE}>{RACK_ID}",'RC01')
        self._node_id = Conf.get(GLOBAL_CONF, f"{CLUSTER}>{SRVNODE}>{NODE_ID}",'SN01')
        self._cluster_id = Conf.get(GLOBAL_CONF, f'{CLUSTER}>{CLUSTER_ID}','CC01')
        self.RESOURCE_TYPE = "node:os:sercive_watchdog"

        specific_info = jsonMsg.get("actuator_request_type").get("service_watchdog_controller")
        service_name = specific_info.get("service_name")
        description = "%s changed its state from %s:%s to %s:%s" % (
                        service_name,
                        specific_info.get("previous_state"),
                        specific_info.get("previous_substate"),
                        specific_info.get("state"),
                        specific_info.get("substate"),
                        )

        info = {
                "site_id": self._site_id,
                "cluster_id": self._cluster_id,
                "rack_id": self._rack_id,
                "node_id": self._node_id,
                "resource_type": self.RESOURCE_TYPE,
                "resource_id": service_name,
                "event_time": epoch_time,
                "description" : description
                }

        json_msg = json.dumps(
            {"sensor_response_type" : {
                    "host_id": host_name,
                    "alert_type": alert_type,
                    "severity": severity,
                    "alert_id": alert_id,
                    "info": info,
                    "specific_info": specific_info
                },
            })

        return json_msg

    def _get_alert_id(self, epoch_time):
        """Returns alert id which is a combination of
           epoch_time and salt value
        """
        salt = str(uuid.uuid4().hex)
        alert_id = epoch_time + salt
        return alert_id

    def _execute_request(self, actuator_instance, json_msg, uuid):
        """Calls perform_request method of an actuator and sends response to
           output channel.
        """
        service_name, state, substate = \
            actuator_instance.perform_request(json_msg)

        if substate:
            result = f"{state}:{substate}"
        else:
            result = state

        self._log_debug(f"_processMsg, service_name: {service_name}, result: {result}")

        # Create an actuator response and send it out
        service_controller_msg = ServiceControllerMsg(service_name, result)
        if uuid is not None:
            service_controller_msg.set_uuid(uuid)
        json_msg = service_controller_msg.getJson()
        self._write_internal_msgQ("RabbitMQegressProcessor", json_msg)

    def suspend(self):
        """Suspends the module thread. It should be non-blocking"""
        super(ServiceMsgHandler, self).suspend()
        self._suspended = True

    def resume(self):
        """Resumes the module thread. It should be non-blocking"""
        super(ServiceMsgHandler, self).resume()
        self._suspended = False

    def shutdown(self):
        """Clean up scheduler queue and gracefully shutdown thread"""
        super(ServiceMsgHandler, self).shutdown()
