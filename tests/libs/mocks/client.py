import functools
import logging
from functools import wraps
from typing import Dict

from eth_utils import decode_hex, encode_hex, is_checksum_address, is_same_address
from web3 import Web3
from web3.contract import Contract, find_matching_event_abi
from web3.utils.events import get_event_data
from web3.utils.filters import construct_event_filter_params

from monitoring_service.states import HashedBalanceProof, MonitorRequest, UnsignedMonitorRequest
from raiden.constants import UINT256_MAX
from raiden.messages import RequestMonitoring, SignedBlindedBalanceProof
from raiden.utils.signer import LocalSigner
from raiden.utils.typing import ChannelID, T_ChannelID, TokenAmount
from raiden_contracts.constants import MessageTypeId
from raiden_libs.types import Address
from raiden_libs.utils import private_key_to_address

log = logging.getLogger(__name__)
NULL_ADDRESS = '0x0000000000000000000000000000000000000000'


def get_event_logs(
    web3: Web3, contract_abi: Dict, event_name: str, from_block: int = 0, to_block: int = None
):
    """Helper function to get all event logs in a given range"""
    abi = find_matching_event_abi(contract_abi, event_name)
    log_data_extract_fn = functools.partial(get_event_data, abi)
    data_filter_set, filter_params = construct_event_filter_params(
        abi, argument_filters=None, fromBlock=from_block, toBlock=to_block
    )

    event_filter = web3.eth.filter(filter_params)
    event_filter.log_entry_formatter = log_data_extract_fn
    event_filter.set_data_filters(data_filter_set)
    event_filter.filter_params = filter_params
    entries = event_filter.get_all_entries()
    web3.eth.uninstallFilter(event_filter.filter_id)
    return entries


def assert_channel_existence(func):
    """Trigger an assert if there is no channel registered with an other participant"""

    @wraps(func)
    def func_wrapper(self, partner_address, *args, **kwargs):
        assert is_checksum_address(partner_address)
        assert partner_address in self.partner_to_channel_id
        return func(self, partner_address, *args, **kwargs)

    return func_wrapper


def sync_channels(func):
    """Synchronize all channels opened with a client"""

    @wraps(func)
    def func_wrapper(self, *args, **kwargs):
        self.partner_to_channel_id = self.sync_open_channels()
        return func(self, *args, **kwargs)

    return func_wrapper


class MockRaidenNode:
    def __init__(
        self,
        privkey: str,
        token_network_contract: Contract,
        token_contract: Contract,
        chain_id: int = 1,
    ) -> None:
        self.privkey = privkey
        self.address = private_key_to_address(privkey)
        self.contract = token_network_contract
        self.token_contract = token_contract
        self.partner_to_channel_id: Dict[Address, ChannelID] = dict()
        self.token_network_abi = None
        self.client_registry: Dict[Address, 'MockRaidenNode'] = dict()
        self.web3 = self.contract.web3
        self.paths_and_fees = None
        self.chain_id = chain_id

    @sync_channels
    def open_channel(self, partner_address: Address) -> ChannelID:
        """Opens channel with a single partner
        Parameters:
            partner_address - a valid ethereum address of the partner
        Return:
            channel_id - id of the channel
        """
        assert is_checksum_address(partner_address)
        assert partner_address in self.client_registry
        # disallow multiple open channels with a same partner
        if partner_address in self.partner_to_channel_id:
            return self.partner_to_channel_id[partner_address]
        # if it doesn't exist, register new channel
        txid = self.contract.functions.openChannel(self.address, partner_address, 15).transact(
            {'from': self.address}
        )
        assert txid is not None
        tx = self.web3.eth.getTransactionReceipt(txid)
        assert tx is not None
        assert len(tx['logs']) == 1
        event = get_event_data(
            find_matching_event_abi(self.contract.abi, 'ChannelOpened'), tx['logs'][0]
        )

        channel_id = event['args']['channel_identifier']
        assert isinstance(channel_id, T_ChannelID)
        assert 0 < channel_id <= UINT256_MAX
        assert is_same_address(event['args']['participant1'], self.address) or is_same_address(
            event['args']['participant2'], self.address
        )
        assert is_same_address(event['args']['participant1'], partner_address) or is_same_address(
            event['args']['participant2'], partner_address
        )

        self.partner_to_channel_id[partner_address] = ChannelID(channel_id)
        self.client_registry[partner_address].open_channel(self.address)

        return ChannelID(channel_id)

    # TODO: maybe change this to a single function that orders the pair
    #   so this node address is first and partner address second
    def get_my_address(self, address1: Address, address2: Address) -> Address:
        """Pick an address that is equal to address of this MockRaidenNode"""
        if is_same_address(self.address, address1):
            return address1
        if is_same_address(self.address, address2):
            return address2
        raise AssertionError

    def get_other_address(self, address1: Address, address2: Address) -> Address:
        """Pick an address that is not equal to address of this MockRaidenNode"""
        if is_same_address(self.address, address1):
            return address2
        if is_same_address(self.address, address2):
            return address1
        raise AssertionError

    def sync_open_channels(self) -> Dict[Address, Dict]:
        """Parses logs and update internal channel state to include all already open channels."""
        entries = get_event_logs(self.web3, self.contract.abi, 'ChannelOpened')
        open_channels = {
            self.get_other_address(x['args']['participant1'], x['args']['participant2']): x[
                'args'
            ]['channel_identifier']
            for x in entries
            if self.address in (x['args']['participant1'], x['args']['participant2'])
        }
        # remove already closed channels from the list
        entries = get_event_logs(self.web3, self.contract.abi, 'ChannelClosed')
        closed_channels = [
            x['args']['channel_identifier']
            for x in entries
            if x['args']['channel_identifier'] in open_channels.keys()
        ]
        return {k: v for k, v in open_channels.items() if k not in closed_channels}

    @assert_channel_existence
    def deposit_to_channel(self, partner_address: Address, amount: int) -> str:
        """Deposits specified amount to an open channel
        Parameters:
            partner_address - address of a partner the client has open channel with
            amount - amount to deposit
        Return:
            transaction hash of the transaction calling `TokenNetwork::setDeposit()` method
        """
        channel_info = self.get_own_channel_info(partner_address)
        self.token_contract.functions.approve(self.contract.address, amount).transact(
            {'from': self.address}
        )
        return self.contract.functions.setTotalDeposit(
            self.partner_to_channel_id[partner_address],
            self.address,
            amount + channel_info['deposit'],
            partner_address,
        ).transact({'from': self.address})

    @assert_channel_existence
    def close_channel(self, partner_address: Address, balance_proof: HashedBalanceProof):
        """Closes an open channel"""
        assert balance_proof is not None
        self.contract.functions.closeChannel(
            self.partner_to_channel_id[partner_address],
            partner_address,
            balance_proof.balance_hash,
            balance_proof.nonce,
            balance_proof.additional_hash,
            balance_proof.signature,
        ).transact({'from': self.address})

    @assert_channel_existence
    def settle_channel(
        self,
        partner_address: Address,
        transferred=tuple(),  # noqa: B008
        locked=tuple(),  # noqa: B008
        locksroot=tuple(),  # noqa: B008
    ):
        """Settles a closed channel. Settling requires that the challenge period is over"""
        assert len(transferred) == 2
        assert len(locked) == 2
        assert len(locksroot) == 2

        # locked + transferred amount of p2 have to be bigger than p1 for the settle call
        # fix order if necessary
        if transferred[0] + locked[0] > transferred[1] + locked[1]:
            self.contract.functions.settleChannel(
                self.partner_to_channel_id[partner_address],
                partner_address,
                transferred[1],
                locked[1],
                locksroot[1],
                self.address,
                transferred[0],
                locked[0],
                locksroot[0],
            ).transact({'from': self.address})
        else:
            self.contract.functions.settleChannel(
                self.partner_to_channel_id[partner_address],
                self.address,
                transferred[0],
                locked[0],
                locksroot[0],
                partner_address,
                transferred[1],
                locked[1],
                locksroot[1],
            ).transact({'from': self.address})

    def get_balance_proof(
        self, partner_address: Address = None, channel_id: int = None, **kwargs
    ) -> HashedBalanceProof:
        """Get a signed balance proof for an open channel.
        Parameters:
            partner_address - address of a partner the node has channel open with
            channel_id - used if `partner_address` is None
            **kwargs - arguments to HashedBalanceProof constructor
        """
        if partner_address is not None:
            assert channel_id is None
            channel_id = self.partner_to_channel_id[partner_address]
        bp = HashedBalanceProof(  # type: ignore  # workaround: mypy complains about priv_key
            channel_identifier=channel_id,
            token_network_address=self.contract.address,
            chain_id=self.chain_id,
            priv_key=self.privkey,
            **kwargs,
        )
        return bp

    def get_monitor_request(
        self, balance_proof: HashedBalanceProof, reward_amount: TokenAmount
    ) -> MonitorRequest:
        """Get monitor request message for a given balance proof."""
        assert balance_proof.signature
        return UnsignedMonitorRequest(
            channel_identifier=balance_proof.channel_identifier,
            token_network_address=balance_proof.token_network_address,
            chain_id=balance_proof.chain_id,
            balance_hash=balance_proof.balance_hash,
            nonce=balance_proof.nonce,
            additional_hash=balance_proof.additional_hash,
            closing_signature=balance_proof.signature,
            reward_amount=reward_amount,
        ).sign(self.privkey)

    def get_request_monitoring(
        self, balance_proof: HashedBalanceProof, reward_amount: TokenAmount
    ) -> RequestMonitoring:
        """Get RequestMonitoring (as sent by the client) message for a given balance proof."""
        assert balance_proof.signature

        non_closing_signer = LocalSigner(decode_hex(self.privkey))
        partner_signed_balance_proof = SignedBlindedBalanceProof(
            channel_identifier=balance_proof.channel_identifier,
            token_network_address=decode_hex(balance_proof.token_network_address),
            nonce=balance_proof.nonce,
            additional_hash=decode_hex(balance_proof.additional_hash),
            chain_id=balance_proof.chain_id,
            signature=decode_hex(balance_proof.signature),
            balance_hash=decode_hex(balance_proof.balance_hash),
        )
        request_monitoring = RequestMonitoring(
            onchain_balance_proof=partner_signed_balance_proof, reward_amount=reward_amount
        )
        request_monitoring.sign(non_closing_signer)
        return request_monitoring

    @assert_channel_existence
    def update_transfer(self, partner_address: Address, balance_proof: HashedBalanceProof):
        """Given a valid signed balance proof, this method calls `updateNonClosingBalanceProof`
        for an open channel
        """
        local_signer = LocalSigner(decode_hex(self.privkey))
        serialized = balance_proof.serialize_bin(msg_type=MessageTypeId.BALANCE_PROOF_UPDATE)
        non_closing_data = serialized + decode_hex(balance_proof.signature)
        non_closing_signature = encode_hex(local_signer.sign(non_closing_data))
        self.contract.functions.updateNonClosingBalanceProof(
            self.partner_to_channel_id[partner_address],
            partner_address,
            self.address,
            balance_proof.balance_hash,
            balance_proof.nonce,
            balance_proof.additional_hash,
            balance_proof.signature,
            non_closing_signature,
        ).transact({'from': self.address})

    @assert_channel_existence
    def get_partner_channel_info(self, partner_address: Address) -> Dict:
        """Return a state of partner's side of the channel, serialized as a dict"""
        return self.get_channel_participant_info(
            self.partner_to_channel_id[partner_address], partner_address, self.address
        )

    @assert_channel_existence
    def get_own_channel_info(self, partner_address: Address) -> Dict:
        """Return a state of our own side of the channel, serialized as a dict"""
        return self.get_channel_participant_info(
            self.partner_to_channel_id[partner_address], self.address, partner_address
        )

    def get_channel_participant_info(
        self, channel_identifier: ChannelID, participant_address: Address, partner_address: Address
    ):
        channel_info = self.contract.functions.getChannelParticipantInfo(
            channel_identifier, participant_address, partner_address
        ).call()
        return_fields = [
            'deposit',
            'withdrawn_amount',
            'is_the_closer',
            'balance_hash',
            'nonce',
            'locksroot',
            'locked_amount',
        ]
        assert len(return_fields) == len(channel_info)
        return {field: channel_info[return_fields.index(field)] for field in return_fields}
