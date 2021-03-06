# Copyright (c) 2020 Seagate Technology LLC and/or its Affiliates
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

# This file contains some utility functions/globals
# commonly used in hw self test
import re
import socket
import subprocess
from cortx.utils.process import SimpleProcess
from framework.utils.conf_utils import (GLOBAL_CONF, Conf,
    NODE_KEY)

def run_cmd(cmd):
    subout = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result = subout.stdout.readlines()
    return result

def get_from_consul(cmd):
    result = run_cmd(cmd)
    return result[0].decode().strip()

def is_virtual():
    cmd = "facter is_virtual"
    retVal = False
    result = run_cmd("facter is_virtual")
    if result:
        if 'true' in result[0].decode():
            retVal = True
    return retVal

def get_node_id():
        node_id = 'srvnode-1'
        try:
            node_id = Conf.get(GLOBAL_CONF, NODE_KEY)
        except Exception as e:
            print(f"Can't read node id, using 'srvnode-1' : {e}")
        return node_id

def get_manufacturer_name():
    """Returns node server manufacturer name.

    Example: Supermicro, Intel Corporation, DELL Inc
    """
    manufacturer = ""
    cmd = "ipmitool bmc info"
    res_op, _, res_rc = SimpleProcess(cmd).run()
    if isinstance(res_op, bytes):
        res_op = res_op.decode("utf-8")
    if res_rc == 0:
        search_res = re.search(
            r"Manufacturer Name[\s]+:[\s]+([\w]+)(.*)", res_op)
        if search_res:
            manufacturer = search_res.groups()[0]
    return manufacturer

def get_server_details():
    """Returns a dictionary of server information using ipmitool.

    Grep 'FRU device description on ID 0' information from the output
    of 'ipmitool fru print'. Server details includes Hostname, Board and
    Product information.
    """
    fru_info = {
        "Host": socket.getfqdn(),
        "Board Mfg": None,
        "Board Product": None,
        "Board Part Number": None,
        "Product Name": None,
        "Product Part Number": None
        }
    cmd = "ipmitool fru print"
    prefix = "FRU Device Description : Builtin FRU Device (ID 0)"
    search_res = ""
    res_op, _, res_rc = SimpleProcess(cmd).run()
    if isinstance(res_op, bytes):
        res_op = res_op.decode("utf-8")
    if res_rc == 0:
        # Get only 'FRU Device Description : Builtin FRU Device (ID 0)' information
        search_res = re.search(r"((.*%s[\S\n\s]+ID 1\)).*)|(.*[\S\n\s]+)" % prefix, res_op)
        if search_res:
            search_res = search_res.group()
    for key in fru_info.keys():
        if key in search_res:
            device_desc = re.search(r"%s[\s]+:[\s]+([\w-]+)(.*)" % key, res_op)
            if device_desc:
                value = device_desc.groups()[0]
            fru_info.update({key: value})
    return fru_info
