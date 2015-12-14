# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function
import logging
import json
import os
import re
import sys

from subprocess32 import CalledProcessError,  Popen, PIPE
from netaddr import IPNetwork, AddrFormatError

from pycalico import netns
from pycalico.netns import Namespace, remove_veth
from pycalico.datastore import DatastoreClient
from pycalico.datastore_errors import MultipleEndpointsMatch
from util import configure_logging
from constants import *

import policy_drivers
from container_engines import DefaultEngine, DockerEngine

# Logging configuration.
LOG_FILENAME = "cni.log"
_log = logging.getLogger(__name__)

# Regex to parse CNI_ARGS.
CNI_ARGS_RE = re.compile("([a-zA-Z0-9/\.\-\_ ]+)=([a-zA-Z0-9/\.\-\_ ]+)(?:;|$)")


class CniPlugin(object):
    """
    Class which encapsulates the function of a CNI plugin.
    """
    def __init__(self, network_config, env):
        self.network_config = network_config
        """
        Network config as provided in the CNI network file passed in
        via stdout.
        """

        self.env = env
        """
        Copy of the environment variable dictionary. Contains CNI_* 
        variables.
        """

        self._client = DatastoreClient()
        """
        DatastoreClient for access to the Calico datastore.
        """

        self.command = env[CNI_COMMAND_ENV]
        """
        The command to execute for this plugin instance. Required. 
        One of:
          - CNI_CMD_ADD
          - CNI_CMD_DELETE
        """

        self.container_id = env[CNI_CONTAINERID_ENV]
        """
        The container's ID in the containerizer. Required.
        """

        self.cni_netns = env[CNI_NETNS_ENV]
        """
        Relative path to the network namespace of this container.
        """

        self.interface = env[CNI_IFNAME_ENV]
        """
        Name of the interface to create within the container.
        """

        self.cni_args = self.parse_cni_args(env[CNI_ARGS_ENV])
        """
        Dictionary of additional CNI arguments provided via
        the CNI_ARGS environment variable.
        """

        self.cni_path = env[CNI_PATH_ENV]
        """
        Path in which to search for CNI plugins.
        """

        self.network_name = network_config["name"]
        """
        Name of the network from the provided network config file.
        """

        self.ipam_result = None
        """
        Stores the output generated by the IPAM plugin.  This is printed
        to stdout at the end of execution.
        """

        self.policy_driver = self._get_policy_driver()
        """
        Chooses the correct policy driver based on the given configuration
        """

        self.container_engine = self._get_container_engine()
        """
        Chooses the correct container engine based on the given configuration.
        """

        # TODO - What config do we need here and how do we get it?
        # self.calico_config = calico_config

    def parse_cni_args(self, cni_args):
        """Parses the given CNI_ARGS string into key value pairs
        and returns a dictionary containing the arguments.

        e.g "FOO=BAR;ABC=123" -> {"FOO": "BAR", "ABC": "123"}

        :param cni_args
        :return: args_to_return - dictionary of parsed cni args
        """
        # Dictionary to return.
        args_to_return = {}

        _log.debug("Parsing CNI_ARGS: %s", cni_args)
        for k,v in CNI_ARGS_RE.findall(cni_args):
            _log.debug("\tParsed CNI_ARG: %s=%s", k, v)
            args_to_return[k.strip()] = v.strip()
        _log.debug("Parsed CNI_ARGS: %s", args_to_return)
        return args_to_return

    def execute(self):
        """Executes this plugin.
        Handles unexpected Exceptions in plugin execution.

        :return The plugin return code.
        """
        rc = 0
        try:
            _log.debug("Starting plugin execution")
            self._execute()
        except SystemExit, e:
            # SystemExit indicates an error that was handled earlier
            # in the stack.  Just set the return code.
            rc = e.code 
        except BaseException:
            # An unexpected Exception has bubbled up - catch it and
            # log it out.
            _log.exception("Unhandled Exception killed plugin")
            rc = 1
        finally:
            _log.debug("Execution complete, rc=%s", rc)
            return rc

    def _execute(self):
        """Private method to execute this plugin.

        Uses the given CNI_COMMAND to determine which action to take.

        :return: None.
        """
        if self.command == CNI_CMD_ADD:
            # TODO - If an add fails, we need to clean up any changes we may
            # have made.
            self.add()
        else:
            assert self.command == CNI_CMD_DELETE, \
                    "Invalid command: %s" % self.command
            self.delete()

    def add(self):
        """"Handles CNI_CMD_ADD requests. 

        Configures Calico networking and prints required json to stdout.

        :return: None.
        """
        # If this container uses host networking, don't network it.
        if self.container_engine.uses_host_networking(self.container_id):
            _log.info("Cannot network container %s since it is configured "
                      "with host networking.", self.container_id)
            sys.exit(0)

        _log.info("Configuring networking for container: %s", 
                  self.container_id)

        # Step 1: Assign an IP address using the given IPAM plugin.
        assigned_ip = self._assign_ip()

        # Step 2: Create the Calico endpoint object.
        endpoint = self._create_endpoint(assigned_ip)

        # Step 3: Provision the veth for this endpoint.
        endpoint = self._provision_veth(endpoint)
        
        # Step 4: Provision / set profile on the created endpoint.
        self.policy_driver.set_profile(endpoint)

        # Step 5: If all successful, print the IPAM plugin's output to stdout.
        dump = json.dumps(self.ipam_result)
        _log.info("Printing CNI result to stdout: %s", dump)
        print(dump)

        _log.info("Finished networking container: %s", self.container_id)
    
    def delete(self):
        """Handles CNI_CMD_DELETE requests.

        Remove this container from Calico networking.

        :return: None.
        """
        _log.info("Remove networking from container: %s", self.container_id)

        # Step 1: Remove any IP assignments.
        self._release_ip()

        # Step 2: Get the Calico endpoint for this workload. If it does not
        # exist, log a warning and exit successfully.
        endpoint = self._get_endpoint()
        if not endpoint:
            _log.info("Endpoint does not exist for container: %s",
                       self.container_id)
            sys.exit(0)

        # Step 3: Delete the veth interface for this endpoint.
        _log.info("Removing veth for endpoint: %s", endpoint.name)
        netns.remove_veth(endpoint.name)

        # Step 4: Delete the Calico endpoint.
        self._remove_endpoint()

        # Step 5: Delete any profiles for this endpoint
        self.policy_driver.remove_profile()

        _log.info("Finished removing container: %s", self.container_id)

    def _assign_ip(self):
        """Assigns and returns an IPv4 address using the IPAM plugin
        specified in the network config file.

        :return: IPAddress - The assigned IP address.
        """
        # Call the IPAM plugin.  Returns the plugin returncode,
        # as well as the CNI result from stdout.
        _log.info("Assigning IP address")
        rc, result = self._call_ipam_plugin()

        if rc:
            # The IPAM plugin failed to assign an IP address. At this point in 
            # execution, we haven't done anything yet, so we don't have to
            # clean up.
            _log.error("IPAM plugin error (rc=%s): %s", rc, result)
            sys.exit(rc)

        try:
            # Load the response and get the assigned IP address.
            self.ipam_result = json.loads(result)
        except ValueError:
            _log.exception("Invalid response from IPAM plugin, exiting")
            # TODO - Make sure IP address is cleaned up.
            sys.exit(1)

        try:
            assigned_ip = IPNetwork(self.ipam_result["ip4"]["ip"])
        except KeyError:
            _log.error("IPAM plugin did not return an IPv4 address")
            # TODO - Make sure IP address is cleaned up.
            sys.exit(1)
        except (AddrFormatError, ValueError):
            # TODO - Make sure IP address is cleaned up.
            _log.error("Invalid IP address %s", self.ipam_result["ip4"]["ip"])
            sys.exit(1)

        _log.info("IPAM plugin assigned IP address: %s", assigned_ip)
        return assigned_ip

    def _release_ip(self):
        """Releases the IP address(es) for this container using the IPAM plugin
        specified in the network config file.

        :return: None.
        """
        _log.debug("Releasing IP address")
        rc, result = self._call_ipam_plugin()

        if rc:
            _log.error("IPAM plugin failed to release IP address")

    def _call_ipam_plugin(self):
        """Calls through to the specified IPAM plugin.
    
        Utilizes the IPAM config as specified in the CNI network
        configuration file.  A dictionary with the following form:
            {
              type: <IPAM TYPE>
            }

        :return: Response from the IPAM plugin.
        """
        # Find the correct plugin based on the given type.
        plugin_path = self._find_ipam_plugin()
        if not plugin_path:
            _log.error("Could not find IPAM plugin of type '%s' in path '%s'",
                       self.network_config['ipam']['type'], self.cni_path)
            sys.exit(1)
    
        # Execute the plugin and return the result.
        _log.info("Using IPAM plugin: %s", plugin_path)
        p = Popen(plugin_path, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        stdout, stderr = p.communicate(json.dumps(self.network_config))
        _log.debug("IPAM plugin return code: %s", p.returncode)
        _log.debug("IPAM plugin output: \nstdout:\n%s\n\nstderr:\n%s\n", 
                   stdout, stderr)
        return p.returncode, stdout

    def _create_endpoint(self, assigned_ip):
        """Creates an endpoint in the Calico datastore with the client.

        :param assigned_ip - IPAddress that has been already allocated
        :return Calico endpoint object
        """
        try:
            endpoint = self._client.create_endpoint(HOSTNAME,
                                                    ORCHESTRATOR_ID,
                                                    self.container_id,
                                                    [assigned_ip])
        except AddrFormatError:
            _log.error("This node is not configured for IPv%s", assigned_ip.version)
            # TODO - call release_ip / cleanup
            sys.exit(1)
        except KeyError:
            _log.error("Unable to create endpoint. BGP configuration not found"
                       " Are the Calico services running?")
            # TODO - call release_ip / cleanup
            sys.exit(1)

        _log.info("Created Calico endpoint with IP address %s", assigned_ip)
        return endpoint

    def _remove_endpoint(self):
        """Removes the given endpoint from the Calico datastore

        :param endpoint:
        :return: None
        """
        try:
            _log.info("Removing endpoint from the Calico datastore")
            self._client.remove_workload(hostname=HOSTNAME,
                                         orchestrator_id=ORCHESTRATOR_ID,
                                         workload_id=self.container_id)
        except KeyError:
            _log.warning("Unable to remove workload with ID %s from datastore",
                       self.container_id)

    def _provision_veth(self, endpoint):
        """Provisions veth for given endpoint.

        Uses the netns relative path passed in through CNI_NETNS_ENV and
        interface passed in through CNI_IFNAME_ENV.

        :param endpoint
        :return Calico endpoint object
        """
        netns_path = os.path.abspath(os.path.join(os.getcwd(), self.cni_netns))
        endpoint.mac = endpoint.provision_veth(Namespace(netns_path),
                                               self.interface)
        self._client.set_endpoint(endpoint)
        _log.info("Provisioned %s in netns %s", self.interface, netns_path)
        return endpoint

    def _get_container_engine(self):
        """Returns a container engine based on the CNI configuration arguments.

        :return: a container engine of type BaseContainerEngine.
        """
        if "K8S_POD_NAME" in self.cni_args:
            _log.debug("Using Kubernetes + Docker container engine")
            return DockerEngine()
        else:
            _log.debug("Using default container engine")
            return DefaultEngine()


    def _get_policy_driver(self):
        """Returns a policy driver based on CNI configuration arguments.

        :return: a policy driver of type BasePolicyDriver
        """
        try:
            self.cni_args["K8S_POD_NAME"]
        except KeyError:
            _log.debug("Using default dolicy driver")
            try:
                driver = policy_drivers.DefaultPolicyDriver(self.network_name)
            except ValueError:
                _log.error("Invalid characters detected in the network name "
                           "'%s'. Only letters a-z, numbers 0-9, and _.- "
                           " are supported", self.network_name)
                sys.exit(1)
        else:
            _log.debug("Using Default Kubernetes Policy Driver")
            driver = policy_drivers.KubernetesDefaultPolicyDriver()

        return driver

    def _get_endpoint(self):
        """Gets endpoint matching the container_id.

        Return None if no endpoint is found.
        Exits with an error if multiple endpoints are found.

        :param container_id:
        :return: Calico endpoint object if found, None if not found
        """
        try:
            _log.info("Retrieving endpoint that matches container ID %s",
                      self.container_id)
            endpoint = self._client.get_endpoint(
                hostname=HOSTNAME,
                orchestrator_id=ORCHESTRATOR_ID,
                workload_id=self.container_id
            )
        except KeyError:
            _log.warning("No endpoint found matching ID %s", self.container_id)
            endpoint = None
        except MultipleEndpointsMatch:
            _log.error("Multiple endpoints found matching ID %s", self.container_id)
            sys.exit(1)

        return endpoint

    def _find_ipam_plugin(self):
        """Locates IPAM plugin binary in plugin path and returns absolute path
        of plugin if found; if not found returns an empty string.

        IPAM plugin type is set in the network config file.
        The plugin path is the CNI path passed through the environment variable
        CNI_PATH.

        :rtype : str
        :return: plugin_path - absolute path of IPAM plugin binary
        """
        plugin_type = self.network_config['ipam']['type']
        plugin_path = ""
        for path in self.cni_path.split(":"):
            _log.debug("Looking for plugin %s in path %s", plugin_type, path)
            temp_path = os.path.abspath(os.path.join(path, plugin_type))
            if os.path.isfile(temp_path):
                _log.debug("Found plugin %s in path %s", plugin_type, path)
                plugin_path = temp_path
                break

        return str(plugin_path)


def main():
    """
    Main function - configures and runs the plugin.
    """
    # Get Calico config from config file.
    # TODO - Is this the correct way to get config in CNI? What config
    # do we need?

    # Configure logging.
    configure_logging(_log, LOG_FILENAME)

    # Get the CNI environment. 
    env = os.environ.copy()
    _log.debug("Loaded environment:\n%s", json.dumps(env, indent=2))

    # Read the network config file from stdin. 
    config_raw = ''.join(sys.stdin.readlines()).replace('\n', '')
    network_config = json.loads(config_raw).copy()
    _log.debug("Loaded network config:\n%s", json.dumps(network_config, indent=2))

    # Create the plugin, passing in the network config, environment,
    # and the Calico configuration options.
    plugin = CniPlugin(network_config, env)

    # Call the CNI plugin.
    sys.exit(plugin.execute())


if __name__ == '__main__': # pragma: no cover
    main()