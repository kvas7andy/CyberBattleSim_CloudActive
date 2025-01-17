# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
    actions.py
    This file contains the class and associated methods for the AgentActions
    class which interacts directly with the environment. It is the class
    which both the user and RL agents should manipulate the environment.
"""

from dataclasses import dataclass
import dataclasses
from datetime import time, datetime
from boolean import boolean
from collections import OrderedDict
import sys
import re
from enum import Enum
from typing import Iterator, List, Optional, Set, Tuple, Dict, TypedDict, cast
from IPython.display import display
import pandas as pd
import numpy as np

from cyberbattle.simulation.model import FirewallRule, MachineStatus, PrivilegeLevel, PropertyName, VulnerabilityID, VulnerabilityType
import cyberbattle.simulation.model as model
from cyberbattle.simulation.config import logger

RewardType = float

DiscoveredNodeInfo = TypedDict('DiscoveredNodeInfo', {
    'id': model.NodeID,
    'status': str
})


class Penalty:
    """Penalties (=negative reward) returned for some actions taken in the simulation"""
    # penalty for generic suspiciousness
    SUPSPICIOUSNESS = -50  # -7.0

    # penalty for attempting a connection to a port that was not open
    SCANNING_UNOPEN_PORT = -10

    # penalty for repeating the same exploit attempt
    REPEAT = -20

    LOCAL_EXPLOIT_FAILED = -20
    FAILED_REMOTE_EXPLOIT = -30

    # penalty for attempting to connect or execute an action on a node that's not in running state
    MACHINE_NOT_RUNNING = 0

    # penalty for attempting a connection with an invalid password
    WRONG_PASSWORD = -10

    # traffic blocked by outoing rule in a local firewall
    BLOCKED_BY_LOCAL_FIREWALL = -10

    # traffic blocked by incoming rule in a remote firewall
    BLOCKED_BY_REMOTE_FIREWALL = -10

    # invalid action (e.g., running an attack from a node that's not owned)
    # (Used only if `throws_on_invalid_actions` is set to False)
    INVALID_ACTION = -5

    # If Precondition needs VPN (ip.local) but profile does not fit
    NO_VPN = -40

    # < 0: agent gets feedback with penalty for triggering deception
    # == 0: agent gets no feedback
    DECEPTION_PENALTY_FOR_AGENT = 0


class Reward:
    # Reward for the first time a local or remote attack
    # gets successfully executed since the last time the target node was imaged.
    # NOTE: the attack cost gets substracted from this reward.
    NEW_SUCCESSFULL_ATTACK_REWARD = 15

    # Fixed reward for discovering a new node
    NODE_DISCOVERED_REWARD = 3

    # Fixed reward for discovering a new credential
    CREDENTIAL_DISCOVERED_REWARD = 3

    # Fixed reward for discovering a new node property
    PROPERTY_DISCOVERED_REWARD = 2

    # Fixed reward for discovering a new profile data, full or partial
    PROFILE_DISCOVERED_REWARD = 3

    # Fixed reward for revealing Reward.SSRF attack with local network disclosure
    IP_CHANGE_TO_IP_LOCAL = 10

    # Fixed reward for using SSRF
    SSRF = 15

    # Define WINNING_REWARD, which substitutes the ending step reward
    WINNING_REWARD = 100.0


class ErrorType(Enum):
    NOERROR = -1
    OTHER = 0
    IP_LOCAL_NEEDED = 1
    ROLES_WRONG = 2
    REPEATED = 3
    PROPERTY_WRONG = 4
    WRONG_AUTH = 5
    NO_AUTH = 6


class EdgeAnnotation(Enum):
    """Annotation added to the network edges created as the simulation is played"""
    KNOWS = 0
    REMOTE_EXPLOIT = 1
    LATERAL_MOVE = 2


@dataclass
class ActionResult:
    """Result from executing an action"""
    reward: Reward
    outcome: Optional[model.VulnerabilityOutcome] = None
    precondition: Optional[model.Precondition] = ""
    profile: Optional[model.Profile] = ""
    reward_string: Optional[str] = ""


ALGEBRA = boolean.BooleanAlgebra()


@dataclass
class NodeTrackingInformation:
    """Track information about nodes gathered throughout the simulation"""
    # Map (vulnid, local_or_remote) to time of last attack.
    # local_or_remote is true for local, false for remote
    last_attack: Dict[Tuple[model.VulnerabilityID, bool, model.Precondition, bool], time] = dataclasses.field(default_factory=dict)
    # Last time the node got owned by the attacker agent
    last_owned_at: Optional[time] = None
    # All node properties discovered so far (indexes of _environment.identifiers.properties without privilege_tags)
    discovered_properties: Set[int] = dataclasses.field(default_factory=set)


class AgentActions:
    """
        This is the AgentActions class. It interacts with and makes changes to the environment.
    """

    def __init__(self, environment: model.Environment, throws_on_invalid_actions=True, deception_penalty_raise=False):
        """
            AgentActions Constructor

        environment               - CyberBattleSim environment parameters
        throws_on_invalid_actions - whether to raise an exception when executing an invalid action (e.g., running an attack from a node that's not owned)
                                    if set to False a negative reward is returned instead.

        """
        self._environment = environment
        self._gathered_credentials: Set[model.CredentialID] = set()
        self._gathered_profiles: List[model.Profile] = [model.Profile(username="NoAuth")]
        self._discovered_nodes: OrderedDict[model.NodeID, NodeTrackingInformation] = OrderedDict()
        self._throws_on_invalid_actions = throws_on_invalid_actions
        self.deception_penalty_raise = False

        # List of all special tags indicating a privilege level reached on a node
        self.privilege_tags = [model.PrivilegeEscalation(p).tag for p in list(PrivilegeLevel)]
        self.__ip_local = False

        # Mark all owned nodes as discovered
        # Mark all initial_properties among node & global proerties as discovered_properties for this node
        for i, node in environment.nodes():
            if node.agent_installed:
                self.__mark_node_as_owned(i, PrivilegeLevel.LocalUser)
                intersect_with_global_properties = list(set(self._environment.identifiers.global_properties).intersection(self._environment.identifiers.initial_properties))
                self.__mark_nodeproperties_as_discovered(i, intersect_with_global_properties)

    def discovered_nodes(self) -> Iterator[Tuple[model.NodeID, model.NodeInfo]]:
        for node_id in self._discovered_nodes:
            yield (node_id, self._environment.get_node(node_id))

    def _check_profile(self, profile: model.Profile, precondition: model.Precondition) -> Tuple[bool, bool, bool]:
        """ This is a quick helper function toc check profile macthcing with precondition, disregarding of properties in precondition
            TODO: change logic of matching, try omit username/id/roles, rather than having True by default,
            because of ~(NOT) in experssion"""
        expr = precondition.expression
        profile_symbols = ALGEBRA.parse(str(profile)).get_symbols()

        true_value = ALGEBRA.parse('true')
        false_value = ALGEBRA.parse('false')
        mapping = {exp_symbol: true_value if not model.Profile.is_profile_symbol(str(exp_symbol)) or exp_symbol in profile_symbols else false_value
                   for exp_symbol in expr.get_symbols()}
        wo_roles_mapping = {exp_symbol: true_value if not model.Profile.is_profile_symbol(str(exp_symbol)) or model.Profile.is_role_symbol(str(exp_symbol)) or exp_symbol in profile_symbols else false_value
                            for exp_symbol in expr.get_symbols()}

        is_true: bool = cast(boolean.Expression, expr.subs(mapping)).simplify() == true_value
        wo_roles_is_true: bool = cast(boolean.Expression, expr.subs(wo_roles_mapping)).simplify() == true_value
        # wo_username_true: bool = False if not is_true and wo_roles_is_true else True
        only_roles_true: bool = np.sum(map(mapping.get, filter(re.compile("roles").match, mapping.keys())))
        return is_true, wo_roles_is_true, only_roles_true

    def _check_properties_after_profile_check(self, target: model.NodeID, profile: model.Profile, precondition: model.Precondition) -> bool:
        """
        This is a quick helper function to check the properties to see if
        they match the ones supplied.
        """
        # node: model.NodeInfo = self._environment.network.nodes[target]['data']
        node_properties = {self._environment.identifiers.properties[p] for p in self.get_discovered_properties(target)}  # only discovered properties, not all ## node.properties

        expr = precondition.expression
        profile_symbols = ALGEBRA.parse(str(profile)).get_symbols()

        true_value = ALGEBRA.parse('true')
        false_value = ALGEBRA.parse('false')
        mapping = {exp_symbol: true_value if str(exp_symbol) in node_properties or model.Profile.is_profile_symbol(str(exp_symbol)) and exp_symbol in profile_symbols else false_value
                   for exp_symbol in expr.get_symbols()}
        is_true: bool = cast(boolean.Expression, expr.subs(mapping)).simplify() == true_value
        return is_true

    def list_vulnerabilities_in_target(
            self,
            target: model.NodeID,
            type_filter: Optional[model.VulnerabilityType] = None) -> List[model.VulnerabilityID]:
        """
            This function takes a model.NodeID for the target to be scanned
            and returns a list of vulnerability IDs.
            It checks each vulnerability in the library against the the properties of a given node
            and determines which vulnerabilities it has.
        """
        if not self._environment.network.has_node(target):
            raise ValueError(f"invalid node id '{target}'")

        target_node_data: model.NodeInfo = self._environment.get_node(target)

        global_vuln: Set[model.VulnerabilityID] = set.union(*([
            model.vuln_name_from_vuln(None, vuln_id, vulnerability)
            for vuln_id, vulnerability in self._environment.vulnerability_library.items()
            if (type_filter is None or vulnerability.type == type_filter)] + [set()])
        )

        local_vuln = set.union(*([
            model.vuln_name_from_vuln(None, vuln_id, vulnerability)
            for vuln_id, vulnerability in target_node_data.vulnerabilities.items()
            if (type_filter is None or vulnerability.type == type_filter)] + [set()])
        )

        return list(global_vuln.union(local_vuln))

    def __annotate_edge(self, source_node_id: model.NodeID,
                        target_node_id: model.NodeID,
                        new_annotation: EdgeAnnotation) -> None:
        """Create the edge if it does not already exist, and annotate with the maximum
        of the existing annotation and a specified new annotation"""
        edge_annotation = self._environment.network.get_edge_data(source_node_id, target_node_id)
        if edge_annotation is not None:
            if 'kind' in edge_annotation:
                new_annotation = EdgeAnnotation(max(edge_annotation['kind'].value, new_annotation.value))
            else:
                new_annotation = EdgeAnnotation(new_annotation.value)
        self._environment.network.add_edge(source_node_id, target_node_id, kind=new_annotation, kind_as_float=float(new_annotation.value))

    def get_discovered_properties(self, node_id: model.NodeID) -> Set[int]:
        return self._discovered_nodes[node_id].discovered_properties

    def __mark_node_as_discovered(self, node_id: model.NodeID, propagate: bool = True) -> int:
        newly_discovered = node_id not in self._discovered_nodes
        newly_discovered_properties = 0

        only_global_properties = set(list(self._discovered_nodes.items())[0][1].discovered_properties).intersection(self._environment.identifiers.global_properties)  # self._discovered_nodes and
        node_info = self._environment.get_node(node_id)
        only_initial_properties = set(node_info.properties).intersection(self._environment.identifiers.initial_properties)
        if propagate and newly_discovered:
            logger.info('discovered node: ' + node_id)
            self._discovered_nodes[node_id] = NodeTrackingInformation()
        newly_discovered_properties = self.__mark_nodeproperties_as_discovered(node_id, only_global_properties.union(only_initial_properties), propagate=propagate)
        return newly_discovered_properties

    def __mark_nodeproperties_as_discovered(self, node_id: model.NodeID, properties: List[PropertyName], propagate: bool = True) -> int:

        # node_info = self._environment.get_node(node_id)

        properties_indices = [self._environment.identifiers.properties.index(p)
                              for p in properties
                              if p not in self.privilege_tags]  # and p in node_info.properties

        if node_id in self._discovered_nodes:
            if not propagate:
                return len(set(properties_indices) - self._discovered_nodes[node_id].discovered_properties)

            before_count = len(self._discovered_nodes[node_id].discovered_properties)
            self._discovered_nodes[node_id].discovered_properties = self._discovered_nodes[node_id].discovered_properties.union(properties_indices)
        else:
            if not propagate:
                return len(properties_indices)

            before_count = 0
            self._discovered_nodes[node_id] = NodeTrackingInformation(discovered_properties=set(properties_indices))

        newly_discovered_properties = len(self._discovered_nodes[node_id].discovered_properties) - before_count
        return newly_discovered_properties

    def __mark_allnodeproperties_as_discovered(self, node_id: model.NodeID, propagate: bool = True):
        node_info: model.NodeInfo = self._environment.network.nodes[node_id]['data']
        return self.__mark_nodeproperties_as_discovered(node_id, node_info.properties, propagate)

    def __mark_node_as_owned(self,
                             node_id: model.NodeID,
                             privilege: PrivilegeLevel = model.PrivilegeLevel.LocalUser,
                             propagate: bool = True) -> Tuple[Optional[time], bool]:
        """Mark a node as owned.
        Return the time it was previously own (or None) and whether it was already owned."""
        node_info = self._environment.get_node(node_id)

        last_owned_at, is_currently_owned = self.__is_node_owned_history(node_id, node_info)

        if propagate and not is_currently_owned:
            if node_id not in self._discovered_nodes:
                self._discovered_nodes[node_id] = NodeTrackingInformation()
            node_info.agent_installed = True
            node_info.privilege_level = model.escalate(node_info.privilege_level, privilege)
            self._environment.network.nodes[node_id].update({'data': node_info})

            self.__mark_allnodeproperties_as_discovered(node_id, propagate)

            # Record that the node just got owned at the current time
            self._discovered_nodes[node_id].last_owned_at = datetime.now()

        return last_owned_at, is_currently_owned

    def __mark_discovered_entities(self, reference_node: model.NodeID, outcome: model.VulnerabilityOutcome,
                                   propagate: bool = True) -> Tuple[int, int, int, int, int, bool]:
        """Mark discovered entities as such and return
        the number of newly discovered nodes, their total value and the number of newly discovered credentials"""
        newly_discovered_nodes = 0
        newly_discovered_nodes_value = 0
        newly_discovered_credentials = 0
        newly_discovered_profiles = 0
        newly_discovered_properties = 0
        ip_local_change = False

        if isinstance(outcome, model.LeakedCredentials):
            for credential in outcome.credentials:
                new_properties = self.__mark_node_as_discovered(credential.node)
                if new_properties:
                    newly_discovered_nodes += 1
                    newly_discovered_nodes_value += self._environment.get_node(credential.node).value
                    newly_discovered_properties += new_properties

                if credential.credential not in self._gathered_credentials:
                    newly_discovered_credentials += 1
                    if propagate:
                        self._gathered_credentials.add(credential.credential)

                if propagate:
                    logger.info('discovered credential: ' + str(credential))
                    self.__annotate_edge(reference_node, credential.node, EdgeAnnotation.KNOWS)

        elif isinstance(outcome, model.LeakedNodesId):
            for node_id in outcome.discovered_nodes:
                new_properties = self.__mark_node_as_discovered(node_id, propagate=propagate)
                if new_properties:
                    newly_discovered_nodes += 1
                    newly_discovered_nodes_value += self._environment.get_node(node_id).value
                    newly_discovered_properties += new_properties

                if propagate:
                    self.__annotate_edge(reference_node, node_id, EdgeAnnotation.KNOWS)

        elif isinstance(outcome, model.LeakedProfiles):
            for profile_str in outcome.discovered_profiles:

                profile_dict = model.profile_str_to_dict(profile_str)
                if "username" not in profile_dict.keys():  # either ip.local OR roles OR id, but only necessary to process is ip.local (below)
                    pass
                    # # TOCHECK maybe that works?
                    # self._gathered_profiles.append(model.Profile(**profile_dict))
                    # # profile.update(profile_dict)
                    # newly_discovered_profiles += len(profile_dict)
                else:
                    if profile_dict["username"] not in [prof.username for prof in self._gathered_profiles]:
                        newly_discovered_profiles += len(profile_dict)
                        if propagate:
                            self._gathered_profiles.append(model.Profile(**profile_dict))
                            if len(profile_dict) > 0:
                                logger.info(f'discovered profile: {profile_str} with N={len(profile_dict)} newly discovered properties ')
                    else:
                        for profile in self._gathered_profiles:
                            if profile_dict["username"] == profile.username:
                                n_updates = profile.update(profile_dict, propagate=propagate)
                                newly_discovered_profiles += n_updates
                                if propagate and n_updates > 0:
                                    logger.info(f'discovered profile: {profile_str} with N={n_updates} newly discovered properties to profile {profile.username}')
                                # for key, value in dataclasses.asdict(profile):
                                #     if value is None and key in profile_dict.keys():
                                #         profile

                if not (self.__ip_local or ip_local_change):
                    ip_local_change = "ip.local" in profile_str

        return newly_discovered_nodes, newly_discovered_nodes_value, newly_discovered_properties, \
            newly_discovered_credentials, newly_discovered_profiles, ip_local_change

    def get_node_privilegelevel(self, node_id: model.NodeID) -> model.PrivilegeLevel:
        """Return the last recorded privilege level of the specified node"""
        node_info = self._environment.get_node(node_id)
        return node_info.privilege_level

    def get_nodes_with_atleast_privilegelevel(self, level: PrivilegeLevel) -> List[model.NodeID]:
        """Return all nodes with at least the specified privilege level"""
        return [n for n, info in self._environment.nodes() if info.privilege_level >= level]

    def is_node_discovered(self, node_id: model.NodeID) -> bool:
        """Returns true if previous actions have revealed the specified node ID"""
        return node_id in self._discovered_nodes

    def __process_outcome(self,
                          expected_type: VulnerabilityType,
                          vulnerability_id: VulnerabilityID,
                          node_id: model.NodeID,  # if local then source_node, if remote than target
                          node_info: model.NodeInfo,  # relates to node_id, source/target node for local/remote
                          local_or_remote: bool,
                          failed_penalty: float,
                          throw_if_vulnerability_not_present: bool,
                          profile: Optional[model.Profile] = None
                          ) -> Tuple[bool, ActionResult]:

        # # logger.info("Process outcome")

        if node_info.status != model.MachineStatus.Running:
            logger.warning("target machine not in running state")
            return False, ActionResult(reward=Penalty.MACHINE_NOT_RUNNING,
                                       outcome=None, profile=str(profile), precondition="", reward_string="")

        is_global_vulnerability = vulnerability_id in self._environment.vulnerability_library
        is_inplace_vulnerability = vulnerability_id in node_info.vulnerabilities

        if is_global_vulnerability:
            vulnerabilities = self._environment.vulnerability_library
        elif is_inplace_vulnerability:
            vulnerabilities = node_info.vulnerabilities
        else:
            if throw_if_vulnerability_not_present:
                raise ValueError(f"Vulnerability '{vulnerability_id}' not supported by node='{node_id}'")
            else:
                # THIS should never occure.
                # It was only possible with target_node being random,
                # now everything is in action_mask, and isinvalid(...) check is done to change exploit -> explore
                logger.warning("Vulnerability '{}' not supported by node '{}'".format(vulnerability_id, node_id))
                return False, ActionResult(reward=Penalty.SUPSPICIOUSNESS, outcome=None, profile=str(profile), precondition="", reward_string="SUSPICIOUSNESS action")

        vulnerability = vulnerabilities[vulnerability_id]

        outcome = vulnerability.outcome
        precondition = vulnerability.precondition

        if vulnerability.type != expected_type:
            raise ValueError(f"vulnerability id '{vulnerability_id}' is for an attack of type {vulnerability.type}, expecting: {expected_type}")

        max_reward, max_outcome, max_precondition_index = -sys.float_info.max, None, -1

        error_type = ErrorType.OTHER
        logger_action = "GOT REWARD r={0} with \tAction: {2}/{1}\tProfile: {3},\tPrecondition: {4}\t Description: {5}"
        error_string_dict = {
            ErrorType.REPEATED: "Repeated action",
            ErrorType.IP_LOCAL_NEEDED: "No access use VPN",
            ErrorType.ROLES_WRONG: "Error {6} only",
            # THIS should be invalid actually, for example, if DOCUMENT is not discovered
            ErrorType.PROPERTY_WRONG: "Not discovered property",
            ErrorType.WRONG_AUTH: "Wrong Authentification",
            ErrorType.NO_AUTH: "Authentification required",
            ErrorType.OTHER: "Cannot get {2}/{1}",
        }

        ip_local_flag = profile.ip == "local" if profile else False  # means we choose to try local network vuln using SSRF
        max_reward_list = []
        max_outcome_list = []
        max_precondition_index_list = []
        error_type_list = []
        need_chemist, need_doctor = False, False

        precond_ind_outcome_str_iter = iter(zip(precondition, range(len(precondition)), outcome)) \
            if isinstance(precondition, list) else iter(zip([precondition], range(1), [outcome]))

        for precondition, precondition_index, outcome in precond_ind_outcome_str_iter:
            reward = -vulnerability.cost

            if "ip.local" in [str(i) for i in precondition.expression.get_symbols() if '.' in str(i)] and \
                    not ip_local_flag:
                if max_reward <= reward + Penalty.NO_VPN:
                    error_type_list.append(ErrorType.IP_LOCAL_NEEDED)
                    max_precondition_index_list.append(precondition_index)
                    max_reward_list.append(reward + Penalty.NO_VPN)
                    max_outcome_list.append(model.ExploitFailed())
                continue

            is_true, wo_roles_is_true, only_roles_true = self._check_profile(profile, precondition)
            if not is_true:
                if max_reward <= reward + Penalty.FAILED_REMOTE_EXPLOIT:
                    error_type_list.append(ErrorType.ROLES_WRONG if wo_roles_is_true else
                                           (ErrorType.NO_AUTH if profile.username == "NoAuth" else ErrorType.WRONG_AUTH))
                    need_doctor, need_chemist = precondition.need_roles()
                    max_precondition_index_list.append(precondition_index)
                    max_reward_list.append(reward + Penalty.FAILED_REMOTE_EXPLOIT)
                    max_outcome_list.append(model.ExploitFailed())
                continue

            # check vulnerability prerequisites
            if not self._check_properties_after_profile_check(node_id, profile, precondition):
                if max_reward <= reward + failed_penalty:
                    error_type_list.append(ErrorType.PROPERTY_WRONG)
                    max_precondition_index_list.append(precondition_index)
                    max_reward_list.append(reward + failed_penalty)
                    max_outcome_list.append(model.ExploitFailed())
                continue

            # Check first if one of outcomes is ExploitFailed
            # ExploitFailed used for 2 cases 1) error (above) 2) deception trigger (here)
            if isinstance(outcome, model.ExploitFailed):
                # reward = -vulnerability.cost
                reward += -outcome.cost if outcome.cost is not None else Penalty.FAILED_REMOTE_EXPLOIT
                # Here process deception reward if we want
                # But we include already possible penalty in outcome == model.DetectionPoint
                # reward += self.deception_penalty_raise * Penalty.DECEPTION_PENALTY_FOR_AGENT * outcome.deception
                if max_reward <= reward:
                    error_type_list.append(ErrorType.OTHER)
                    max_precondition_index_list.append(precondition_index)
                    max_reward_list.append(reward)
                    max_outcome_list.append(outcome)
                continue

            # if the vulnerability type is a privilege escalation
            # and if the escalation level is not already reached on that node,
            # then add the escalation tag to the node properties
            if isinstance(outcome, model.PrivilegeEscalation):
                if outcome.tag in node_info.properties:
                    reward += Penalty.REPEAT
                else:
                    last_owned_at, _ = self.__mark_node_as_owned(node_id, outcome.level, propagate=False)
                    if not last_owned_at:
                        reward += float(node_info.value)

                    # TOCHECK Here should be also new properties count
                    node_info.properties.append(outcome.tag)

            elif isinstance(outcome, model.LateralMove):
                last_owned_at, _ = self.__mark_node_as_owned(node_id, propagate=False)
                if not last_owned_at:
                    reward += float(node_info.value)

            elif isinstance(outcome, model.CustomerData):
                reward += outcome.reward

            elif isinstance(outcome, model.DetectionPoint):
                reward += Penalty.DECEPTION_PENALTY_FOR_AGENT

            # Dummy update all entites, for reward evaluation
            newly_discovered_nodes, \
                discovered_nodes_value, \
                newly_discovered_properties, \
                newly_discovered_credentials, \
                newly_discovered_profiles, ip_local_change = self.__mark_discovered_entities(node_id, outcome, propagate=False)

            if isinstance(outcome, model.ProbeSucceeded):
                only_global_properties = set(outcome.discovered_properties).intersection(self._environment.identifiers.global_properties)

                for p in outcome.discovered_properties:
                    assert p in node_info.properties or p in self._environment.identifiers.global_properties, \
                        f'Discovered property {p} must belong to the set of properties associated with the node or global properties.'

                newly_discovered_properties += self.__mark_nodeproperties_as_discovered(node_id, outcome.discovered_properties, propagate=False)
                for discovered_node_id in self._discovered_nodes:
                    self.__mark_nodeproperties_as_discovered(discovered_node_id, only_global_properties, propagate=False)

            # TOCHECK should be never true, as once we discover node_id, we should input it,
            # if target_node_id is not inside desicovered_nodes yet,
            # 1) action_mask, shoudl have excluded it 2) exploit_remote_vulnerability excludes it

            already_executed = False
            if node_id in self._discovered_nodes:
                lookup_key = (vulnerability_id, local_or_remote, precondition, True)
                already_executed = lookup_key in self._discovered_nodes[node_id].last_attack

            if already_executed:
                last_time = self._discovered_nodes[node_id].last_attack[lookup_key]
                if node_info.last_reimaging is None or last_time >= node_info.last_reimaging:
                    if max_reward <= Penalty.REPEAT - vulnerability.cost:
                        error_type_list.append(ErrorType.REPEATED)
                        max_precondition_index_list.append(precondition_index)
                        max_reward_list.append(Penalty.REPEAT - vulnerability.cost)
                        max_outcome_list.append(outcome)
                    continue
            elif not isinstance(outcome, model.ExploitFailed):
                reward += Reward.NEW_SUCCESSFULL_ATTACK_REWARD

            if not self.__ip_local and ip_local_change:
                reward += Reward.IP_CHANGE_TO_IP_LOCAL

            # Note: `discovered_nodes_value` should not be added to the reward
            # unless the discovered nodes got owned, but this case is already covered above
            reward += newly_discovered_nodes * Reward.NODE_DISCOVERED_REWARD
            reward += newly_discovered_credentials * Reward.CREDENTIAL_DISCOVERED_REWARD
            reward += newly_discovered_profiles * Reward.PROFILE_DISCOVERED_REWARD
            reward += newly_discovered_properties * Reward.PROPERTY_DISCOVERED_REWARD

            if "ip.local" in str(precondition.expression) and ip_local_flag:
                reward += Reward.SSRF

            if max_reward <= reward:
                # print(newly_discovered_nodes, newly_discovered_credentials, newly_discovered_profiles, reward)
                error_type_list.append(ErrorType.NOERROR)
                max_reward_list.append(reward)
                max_precondition_index_list.append(precondition_index)
                max_outcome_list.append(outcome)  # vulnerability.outcome[max_precondition_index] if isinstance(vulnerability.outcome, list) else vulnerability.outcome

            max_reward = max(max_reward_list)

        ind_max_reward_candidates = np.argwhere(max_reward_list == np.amax(max_reward_list)).flatten()
        ind_max_reward = np.random.choice(ind_max_reward_candidates)
        max_reward, max_outcome, error_type, max_precondition_index = max_reward_list[ind_max_reward], max_outcome_list[ind_max_reward], \
            error_type_list[ind_max_reward], max_precondition_index_list[ind_max_reward]
        # max_outcome = vulnerability.outcome[max_precondition_index] if isinstance(vulnerability.outcome, list) else vulnerability.outcome
        max_reward_string = vulnerability.reward_string[max_precondition_index] if isinstance(vulnerability.reward_string, list) else vulnerability.reward_string
        max_precondition = vulnerability.precondition[max_precondition_index] if isinstance(vulnerability.precondition, list) else vulnerability.precondition
        if len(ind_max_reward_candidates) > 1:
            logger.warning(f"\tChoosing candidate max_reward with node {node_id} precondition  {str(max_precondition.expression)} among other preconditions indices {ind_max_reward_candidates}")

        if error_type != ErrorType.NOERROR:  # ver2: error_type == ErrorType.NOERROR ver3: max_reward < 0
            if error_type != ErrorType.REPEATED:
                lookup_key = (vulnerability_id, local_or_remote, max_precondition, False)

                already_executed = node_id in self._discovered_nodes and lookup_key in self._discovered_nodes[node_id].last_attack
                if already_executed:
                    last_time = self._discovered_nodes[node_id].last_attack[lookup_key]
                    if node_info.last_reimaging is None or last_time >= node_info.last_reimaging:
                        error_type = ErrorType.REPEATED
                        max_reward -= Penalty.REPEAT

            logger.warning((error_string_dict[error_type] + " => " + logger_action).format(max_reward, vulnerability_id, node_id, str(profile), str(max_precondition.expression),
                                                                                           max_reward_string, need_doctor * "doctors or " + (need_doctor + need_chemist) * "chemists"))
            return False, ActionResult(reward=max_reward, outcome=max_outcome, profile=profile,
                                       precondition=max_precondition, reward_string=max_reward_string)

        logger.info(logger_action.format(max_reward, vulnerability_id, node_id, str(profile), str(max_precondition.expression), max_reward_string))

        reward = -vulnerability.cost
        if isinstance(max_outcome, model.PrivilegeEscalation):
            if max_outcome.tag in node_info.properties:
                reward += Penalty.REPEAT
            else:
                last_owned_at, is_currently_owned = self.__mark_node_as_owned(node_id, max_outcome.level)
                if not last_owned_at:
                    reward += float(node_info.value)
                node_info.properties.append(max_outcome.tag)

        elif isinstance(max_outcome, model.LateralMove):
            last_owned_at, is_currently_owned = self.__mark_node_as_owned(node_id)
            if not last_owned_at:
                reward += float(node_info.value)

        elif isinstance(outcome, model.CustomerData):
            reward += outcome.reward

        elif isinstance(max_outcome, model.DetectionPoint):
            reward += Penalty.DECEPTION_PENALTY_FOR_AGENT

            # Update all entites
        newly_discovered_nodes, \
            discovered_nodes_value, \
            newly_discovered_properties, \
            newly_discovered_credentials, \
            newly_discovered_profiles, ip_local_change = self.__mark_discovered_entities(node_id, max_outcome)

        if isinstance(max_outcome, model.ProbeSucceeded):
            only_global_properties = set(max_outcome.discovered_properties).intersection(self._environment.identifiers.global_properties)

            for p in max_outcome.discovered_properties:
                assert p in node_info.properties or p in self._environment.identifiers.global_properties, \
                    f'Discovered property {p} must belong to the set of properties associated with the node or global properties.'

            newly_discovered_properties += self.__mark_nodeproperties_as_discovered(node_id, outcome.discovered_properties)
            for discovered_node_id in self._discovered_nodes:
                self.__mark_nodeproperties_as_discovered(discovered_node_id, only_global_properties)

        already_executed = False
        if node_id in self._discovered_nodes:
            lookup_key = (vulnerability_id, local_or_remote, max_precondition, True)
            already_executed = lookup_key in self._discovered_nodes[node_id].last_attack

        if already_executed:
            last_time = self._discovered_nodes[node_id].last_attack[lookup_key]
            if node_info.last_reimaging is None or last_time >= node_info.last_reimaging:
                # should not come to REPEAT
                pass
        elif not isinstance(outcome, model.ExploitFailed):
            reward += Reward.NEW_SUCCESSFULL_ATTACK_REWARD

        self._discovered_nodes[node_id].last_attack[lookup_key] = datetime.now()

        if ip_local_change:
            self.__ip_local = True
            reward += Reward.IP_CHANGE_TO_IP_LOCAL
            logger.info("Gained access to local network (possible to exploit SSRF)!")

        reward += newly_discovered_nodes * Reward.NODE_DISCOVERED_REWARD
        reward += newly_discovered_credentials * Reward.CREDENTIAL_DISCOVERED_REWARD
        reward += newly_discovered_profiles * Reward.PROFILE_DISCOVERED_REWARD
        reward += newly_discovered_properties * Reward.PROPERTY_DISCOVERED_REWARD

        if "ip.local" in str(max_precondition.expression) and ip_local_flag:
            reward += Reward.SSRF
            logger.info("Exploiting SSRF for access to endpoints through local network!")

        assert reward == max_reward, f'{reward} and {max_reward}, action {node_id} {str(max_precondition.expression)} {str(type(max_outcome))}'

        return True, ActionResult(reward=max_reward, outcome=max_outcome, profile=profile,
                                  precondition=max_precondition, reward_string=max_reward_string)

    def exploit_remote_vulnerability(self,
                                     node_id: model.NodeID,
                                     target_node_id: model.NodeID,
                                     profile: model.Profile,
                                     vulnerability_variable_id: model.VulnerabilityID
                                     ) -> ActionResult:
        """
        Attempt to exploit a remote vulnerability
        from a source node to another node using the specified
        vulnerability.
        """
        if node_id not in self._environment.network.nodes:
            raise ValueError(f"invalid node id '{node_id}'")
        if target_node_id not in self._environment.network.nodes:
            raise ValueError(f"invalid target node id '{target_node_id}'")

        source_node_info: model.NodeInfo = self._environment.get_node(node_id)
        target_node_info: model.NodeInfo = self._environment.get_node(target_node_id)

        if not source_node_info.agent_installed:
            if self._throws_on_invalid_actions:
                raise ValueError("Agent does not owned the source node '" + node_id + "'")
            else:
                return ActionResult(reward=Penalty.INVALID_ACTION, outcome=None, profile=str(profile))

        if target_node_id not in self._discovered_nodes:
            if self._throws_on_invalid_actions:
                raise ValueError("Agent has not discovered the target node '" + target_node_id + "'")
            else:
                return ActionResult(reward=Penalty.INVALID_ACTION, outcome=None, profile=str(profile))

        succeeded, result = self.__process_outcome(
            model.VulnerabilityType.REMOTE,
            vulnerability_variable_id,
            target_node_id,
            target_node_info,
            profile=profile,
            local_or_remote=False,
            failed_penalty=Penalty.FAILED_REMOTE_EXPLOIT,
            # We do not throw if the vulnerability is missing in order to
            # allow agent attempts to explore potential remote vulnerabilities
            throw_if_vulnerability_not_present=False
        )

        if succeeded:
            self.__annotate_edge(node_id, target_node_id, EdgeAnnotation.REMOTE_EXPLOIT)

        return result

    def exploit_local_vulnerability(self, node_id: model.NodeID,
                                    vulnerability_id: model.VulnerabilityID) -> ActionResult:
        """
            This function exploits a local vulnerability on a node
            it takes a nodeID for the target and a vulnerability ID.

            It returns either a vulnerabilityoutcome object or None
        """
        graph = self._environment.network
        if node_id not in graph.nodes:
            raise ValueError(f"invalid node id '{node_id}'")

        node_info = self._environment.get_node(node_id)

        if not node_info.agent_installed:
            if self._throws_on_invalid_actions:
                raise ValueError(f"Agent does not owned the node '{node_id}'")
            else:
                return ActionResult(reward=Penalty.INVALID_ACTION, outcome=None)

        succeeded, result = self.__process_outcome(
            model.VulnerabilityType.LOCAL,
            vulnerability_id,
            node_id, node_info,
            local_or_remote=True,
            failed_penalty=Penalty.LOCAL_EXPLOIT_FAILED,
            throw_if_vulnerability_not_present=False)

        return result

    def __is_passing_firewall_rules(self, rules: List[model.FirewallRule], port_name: model.PortName) -> bool:
        """Determine if traffic on the specified port is permitted by the specified sets of firewall rules"""
        for rule in rules:
            if rule.port == port_name:
                if rule.permission == model.RulePermission.ALLOW:
                    return True
                else:
                    logger.debug(f'BLOCKED TRAFFIC - PORT \'{port_name}\' Reason: ' + rule.reason)
                    return False

        logger.debug(f"BLOCKED TRAFFIC - PORT '{port_name}' - Reason: no rule defined for this port.")
        return False

    def __is_node_owned_history(self, target_node_id, target_node_data):
        """ Returns the last time the node got owned and whether it is still currently owned."""
        last_previously_owned_at = self._discovered_nodes[target_node_id].last_owned_at if target_node_id in self._discovered_nodes else None

        is_currently_owned = last_previously_owned_at is not None and \
            (target_node_data.last_reimaging is None or last_previously_owned_at >= target_node_data.last_reimaging)
        return last_previously_owned_at, is_currently_owned

    def connect_to_remote_machine(
            self,
            source_node_id: model.NodeID,
            target_node_id: model.NodeID,
            port_name: model.PortName,
            credential: model.CredentialID) -> ActionResult:
        """
            This function connects to a remote machine with credential as opposed to via an exploit.
            It takes a NodeId for the source machine, a NodeID for the target Machine, and a credential object
            for the credential.
        """
        graph = self._environment.network
        if source_node_id not in graph.nodes:
            raise ValueError(f"invalid node id '{source_node_id}'")
        if target_node_id not in graph.nodes:
            raise ValueError(f"invalid node id '{target_node_id}''")

        target_node = self._environment.get_node(target_node_id)
        source_node = self._environment.get_node(source_node_id)
        # ensures that the source node is owned by the agent
        # and that the target node is discovered

        if not source_node.agent_installed:
            if self._throws_on_invalid_actions:
                raise ValueError(f"Agent does not owned the source node '{source_node_id}'")
            else:
                return ActionResult(reward=Penalty.INVALID_ACTION, outcome=None)

        if target_node_id not in self._discovered_nodes:
            if self._throws_on_invalid_actions:
                raise ValueError(f"Agent has not discovered the target node '{target_node_id}'")
            else:
                return ActionResult(reward=Penalty.INVALID_ACTION, outcome=None)

        if credential not in self._gathered_credentials:
            if self._throws_on_invalid_actions:
                raise ValueError(f"Agent has not discovered credential '{credential}'")
            else:
                return ActionResult(reward=Penalty.INVALID_ACTION, outcome=None)

        if not self.__is_passing_firewall_rules(source_node.firewall.outgoing, port_name):
            logger.info(f"BLOCKED TRAFFIC: source node '{source_node_id}'" +
                        f" is blocking outgoing traffic on port '{port_name}'")
            return ActionResult(reward=Penalty.BLOCKED_BY_LOCAL_FIREWALL,
                                outcome=None)

        if not self.__is_passing_firewall_rules(target_node.firewall.incoming, port_name):
            logger.info(f"BLOCKED TRAFFIC: target node '{target_node_id}'" +
                        f" is blocking outgoing traffic on port '{port_name}'")
            return ActionResult(reward=Penalty.BLOCKED_BY_REMOTE_FIREWALL,
                                outcome=None)

        target_node_is_listening = port_name in [i.name for i in target_node.services]
        if not target_node_is_listening:
            logger.info(f"target node '{target_node_id}' not listening on port '{port_name}'")
            return ActionResult(reward=Penalty.SCANNING_UNOPEN_PORT,
                                outcome=None)
        else:
            target_node_data: model.NodeInfo = self._environment.get_node(target_node_id)

            if target_node_data.status != model.MachineStatus.Running:
                logger.info("target machine not in running state")
                return ActionResult(reward=Penalty.MACHINE_NOT_RUNNING,
                                    outcome=None)

            # check the credentials before connecting
            if not self._check_service_running_and_authorized(target_node_data, port_name, credential):
                logger.info("invalid credentials supplied")
                return ActionResult(reward=Penalty.WRONG_PASSWORD,
                                    outcome=None)

            last_owned_at, is_already_owned = self.__mark_node_as_owned(target_node_id)

            if is_already_owned:
                return ActionResult(reward=Penalty.REPEAT, outcome=model.LateralMove())

            if target_node_id not in self._discovered_nodes:
                self._discovered_nodes[target_node_id] = NodeTrackingInformation()

            self.__annotate_edge(source_node_id, target_node_id, EdgeAnnotation.LATERAL_MOVE)

            logger.info(f"Infected node '{target_node_id}' from '{source_node_id}'" +
                        f" via {port_name} with credential '{credential}'")
            if target_node.owned_string:
                logger.info("Owned message: " + target_node.owned_string)

            return ActionResult(reward=float(target_node_data.value) if last_owned_at is None else 0.0,
                                outcome=model.LateralMove())

    def _check_service_running_and_authorized(self,
                                              target_node_data: model.NodeInfo,
                                              port_name: model.PortName,
                                              credential: model.CredentialID) -> bool:
        """
            This is a quick helper function to check the prerequisites to see if
            they match the ones supplied.
        """
        for service in target_node_data.services:
            if service.running and service.name == port_name and credential in service.allowedCredentials:
                return True
        return False

    def list_nodes(self) -> List[DiscoveredNodeInfo]:
        """Returns the list of nodes ID that were discovered or owned by the attacker."""
        return [cast(DiscoveredNodeInfo, {'id': node_id,
                                          'status': 'owned' if node_info.agent_installed else 'discovered'
                                          })
                for node_id, node_info in self.discovered_nodes()
                ]

    def list_remote_attacks(self, node_id: model.NodeID) -> List[model.VulnerabilityID]:
        """Return list of all remote attacks that may be executed onto the specified node."""
        attacks: List[model.VulnerabilityID] = self.list_vulnerabilities_in_target(
            node_id, model.VulnerabilityType.REMOTE)
        return attacks

    def list_local_attacks(self, node_id: model.NodeID) -> List[model.VulnerabilityID]:
        """Return list of all local attacks that may be executed onto the specified node."""
        attacks: List[model.VulnerabilityID] = self.list_vulnerabilities_in_target(
            node_id, model.VulnerabilityType.LOCAL)
        return attacks

    def list_attacks(self, node_id: model.NodeID) -> List[model.VulnerabilityID]:
        """Return list of all attacks that may be executed on the specified node."""
        attacks: List[model.VulnerabilityID] = self.list_vulnerabilities_in_target(
            node_id)
        return attacks

    def list_all_attacks(self) -> List[Dict[str, object]]:
        """List all possible attacks from all the nodes currently owned by the attacker"""
        iter_profiles = iter(self._gathered_profiles)
        on_owned_nodes: List[Dict[str, object]] = [
            {'id': n['id'],
             'internal index': i,
             'status': n['status'],
             'properties': self._environment.get_node(n['id']).properties,
             'discovered node properties': list(map(self._environment.identifiers.properties.__getitem__, self.get_discovered_properties(n['id']))),
             'local_attacks': self.list_local_attacks(n['id']),
             'remote_attacks': self.list_remote_attacks(n['id']),
             'gathered_credentials': self._gathered_credentials,
             'discovered profiles': next(iter_profiles, "")
             }
            for i, n in enumerate(self.list_nodes()) if n['status'] == 'owned']
        on_discovered_nodes: List[Dict[str, object]] = [{'id': n['id'],
                                                         'internal index': i,
                                                        'status': n['status'],
                                                         'properties': self._environment.get_node(n['id']).properties,
                                                         'discovered node properties': list(map(self._environment.identifiers.properties.__getitem__, self.get_discovered_properties(n['id']))),
                                                         'local_attacks': None,
                                                         'remote_attacks': self.list_remote_attacks(n['id']),
                                                         'gathered_credentials': self._gathered_credentials,
                                                         'discovered profiles': next(iter_profiles, "")}
                                                        for i, n in enumerate(self.list_nodes()) if n['status'] != 'owned']
        return on_owned_nodes + on_discovered_nodes

    def print_all_attacks(self, filename=None) -> None:
        """Pretty print list of all possible attacks from all the nodes currently owned by the attacker"""
        df = pd.DataFrame.from_dict(self.list_all_attacks()).set_index('id')
        if filename:
            df.to_csv(filename, index=False)
            display(df)  # type: ignore


class DefenderAgentActions:
    """Actions reserved to defender agents"""

    # Number of steps it takes to completely reimage a node
    REIMAGING_DURATION = 15

    def __init__(self, environment: model.Environment):
        # map nodes being reimaged to the remaining number of steps to completion
        self.node_reimaging_progress: Dict[model.NodeID, int] = dict()

        # Last calculated availability of the network
        self.__network_availability: float = 1.0

        self._environment = environment

    @ property
    def network_availability(self):
        return self.__network_availability

    def reimage_node(self, node_id: model.NodeID):
        """Re-image a computer node"""
        # Mark the node for re-imaging and make it unavailable until re-imaging completes
        self.node_reimaging_progress[node_id] = self.REIMAGING_DURATION

        node_info = self._environment.get_node(node_id)
        assert node_info.reimagable, f'Node {node_id} is not re-imageable'

        node_info.agent_installed = False
        node_info.privilege_level = model.PrivilegeLevel.NoAccess
        node_info.status = model.MachineStatus.Imaging
        node_info.last_reimaging = datetime.now()
        self._environment.network.nodes[node_id].update({'data': node_info})

    def on_attacker_step_taken(self):
        """Function to be called each time a step is take in the simulation"""
        for node_id in list(self.node_reimaging_progress.keys()):
            remaining_steps = self.node_reimaging_progress[node_id]
            if remaining_steps > 0:
                self.node_reimaging_progress[node_id] -= 1
            else:
                logger.info(f"Machine re-imaging completed: {node_id}")
                node_data = self._environment.get_node(node_id)
                node_data.status = model.MachineStatus.Running
                self.node_reimaging_progress.pop(node_id)

        # Calculate the network availability metric based on machines
        # and services that are running
        total_node_weights = 0
        network_node_availability = 0
        for node_id, node_info in self._environment.nodes():
            total_service_weights = 0
            running_service_weights = 0
            for service in node_info.services:
                total_service_weights += service.sla_weight
                running_service_weights += service.sla_weight * int(service.running)

            if node_info.status == MachineStatus.Running:
                adjusted_node_availability = (1 + running_service_weights) / (1 + total_service_weights)
            else:
                adjusted_node_availability = 0.0

            total_node_weights += node_info.sla_weight
            network_node_availability += adjusted_node_availability * node_info.sla_weight

        self.__network_availability = network_node_availability / total_node_weights
        assert (self.__network_availability <= 1.0 and self.__network_availability >= 0.0)

    def override_firewall_rule(self, node_id: model.NodeID, port_name: model.PortName, incoming: bool, permission: model.RulePermission):
        node_data = self._environment.get_node(node_id)

        def add_or_patch_rule(rules) -> List[FirewallRule]:
            new_rules = []
            has_matching_rule = False
            for r in rules:
                if r.port == port_name:
                    has_matching_rule = True
                    new_rules.append(FirewallRule(r.port, permission))
                else:
                    new_rules.append(r)

            if not has_matching_rule:
                new_rules.append(model.FirewallRule(port_name, permission))
            return new_rules

        if incoming:
            node_data.firewall.incoming = add_or_patch_rule(node_data.firewall.incoming)
        else:
            node_data.firewall.outgoing = add_or_patch_rule(node_data.firewall.outgoing)

    def block_traffic(self, node_id: model.NodeID, port_name: model.PortName, incoming: bool):
        return self.override_firewall_rule(node_id, port_name, incoming, permission=model.RulePermission.BLOCK)

    def allow_traffic(self, node_id: model.NodeID, port_name: model.PortName, incoming: bool):
        return self.override_firewall_rule(node_id, port_name, incoming, permission=model.RulePermission.ALLOW)

    def stop_service(self, node_id: model.NodeID, port_name: model.PortName):
        node_data = self._environment.get_node(node_id)
        assert node_data.status == model.MachineStatus.Running, "Machine must be running to stop a service"
        for service in node_data.services:
            if service.name == port_name:
                service.running = False

    def start_service(self, node_id: model.NodeID, port_name: model.PortName):
        node_data = self._environment.get_node(node_id)
        assert node_data.status == model.MachineStatus.Running, "Machine must be running to start a service"
        for service in node_data.services:
            if service.name == port_name:
                service.running = True
