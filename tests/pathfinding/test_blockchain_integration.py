"""
The test in this module uses the mocked raiden client to create blockchain events and
processes them. Additionally, it mocks the transport layer directly. It tests the
interaction of many moving parts - yet, it is currently really slow.
Therefore, usually mocked_integration should be used.
"""
from typing import List
from unittest.mock import Mock, patch

import gevent

from pathfinding_service import PathfindingService
from pathfinding_service.config import DEFAULT_REVEAL_TIMEOUT
from pathfinding_service.model import ChannelView
from raiden.utils.typing import BlockNumber
from raiden_contracts.constants import CONTRACT_TOKEN_NETWORK_REGISTRY, CONTRACT_USER_DEPOSIT
from raiden_contracts.contract_manager import ContractManager


def test_pfs_with_mocked_client(
    web3,
    ethereum_tester,
    contracts_manager: ContractManager,
    token_network_registry_contract,
    channel_descriptions_case_1: List,
    generate_raiden_clients,
    wait_for_blocks,
    user_deposit_contract,
):
    """ Instantiates some MockClients and the PathfindingService.

    Mocks blockchain events to setup a token network with a given topology, specified in
    the channel_description fixture. Tests all PFS methods w.r.t. to that topology
    """
    clients = generate_raiden_clients(7)
    token_network_address = clients[0].contract.address

    with patch('pathfinding_service.service.MatrixListener', new=Mock):
        pfs = PathfindingService(
            web3=web3,
            contracts={
                CONTRACT_TOKEN_NETWORK_REGISTRY: token_network_registry_contract,
                CONTRACT_USER_DEPOSIT: user_deposit_contract,
            },
            required_confirmations=1,
            db_filename=':memory:',
            poll_interval=0.1,
            sync_start_block=BlockNumber(0),
            private_key='3a1076bf45ab87712ad64ccb3b10217737f7faacbf2872e88fdd9a537d8fe266',
        )

    # greenlet needs to be started and context switched to
    pfs.start()
    wait_for_blocks(1)
    gevent.sleep(0.1)

    # there should be one token network registered
    assert len(pfs.token_networks) == 1

    token_network = pfs.token_networks[token_network_address]
    graph = token_network.G
    channel_identifiers = []
    for (
        p1_index,
        p1_deposit,
        _p1_capacity,
        _p1_fee,
        _p1_reveal_timeout,
        p2_index,
        p2_deposit,
        _p2_capacity,
        _p2_fee,
        _p2_reveal_timeout,
        _settle_timeout,
    ) in channel_descriptions_case_1:
        # order is important here because we check order later
        channel_identifier = clients[p1_index].open_channel(clients[p2_index].address)
        channel_identifiers.append(channel_identifier)

        clients[p1_index].deposit_to_channel(clients[p2_index].address, p1_deposit)
        clients[p2_index].deposit_to_channel(clients[p1_index].address, p2_deposit)
        gevent.sleep()
    wait_for_blocks(1)
    gevent.sleep(0.1)

    # there should be as many open channels as described
    assert len(token_network.channel_id_to_addresses.keys()) == len(channel_descriptions_case_1)

    # check that deposits, settle_timeout and transfers got registered
    for (
        index,
        (
            _p1_index,
            p1_deposit,
            _p1_capacity,
            _p1_fee,
            _p1_reveal_timeout,
            _p2_index,
            p2_deposit,
            _p2_capacity,
            _p2_fee,
            _p2_reveal_timeout,
            _settle_timeout,
        ),
    ) in enumerate(channel_descriptions_case_1):
        channel_identifier = channel_identifiers[index]
        p1_address, p2_address = token_network.channel_id_to_addresses[channel_identifier]
        view1: ChannelView = graph[p1_address][p2_address]['view']
        view2: ChannelView = graph[p2_address][p1_address]['view']
        assert view1.deposit == p1_deposit
        assert view2.deposit == p2_deposit
        assert view1.settle_timeout == 15
        assert view2.settle_timeout == 15
        assert view1.reveal_timeout == DEFAULT_REVEAL_TIMEOUT
        assert view2.reveal_timeout == DEFAULT_REVEAL_TIMEOUT
    # now close all channels
    for (
        p1_index,
        _p1_deposit,
        _p1_capacity,
        _p1_fee,
        _p1_reveal_timeout,
        p2_index,
        _p2_deposit,
        _p2_capacity,
        _p2_fee,
        _p2_reveal_timeout,
        _settle_timeout,
    ) in channel_descriptions_case_1:
        balance_proof = clients[p2_index].get_balance_proof(
            clients[p1_index].address,
            nonce=1,
            transferred_amount=0,
            locked_amount=0,
            locksroot='0x%064x' % 0,
            additional_hash='0x%064x' % 1,
        )
        clients[p1_index].close_channel(clients[p2_index].address, balance_proof)

    wait_for_blocks(1)
    gevent.sleep(0.1)

    # there should be no channels
    assert len(token_network.channel_id_to_addresses.keys()) == 0
    pfs.stop()
