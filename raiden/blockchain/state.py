"""Module for functions that load additional data necessary to instantiate the
ContractReceive* state changes, i.e. state changes for the blockchain events.

It is *very* important that every function here only fetches **confirmed** data,
otherwise the node will be susceptible to races due to reorgs. These races can
crash the client in the best case, or be an attack vector in the worst case.
Because of this, the event itself must already be confirmed.

If possible, the confirmed data should be retrievied from the same block at
which the event was emitted. However, because of state pruning this is not
always possible. If that block is pruned then the latest confirmed block must
be used.

Note that the latest confirmed block is *not necessarily* the same as the
current block number in the state machine. The current block number in the
ChainState is *a* confirmed block number, but not necessarily the latest. This
distinction is important during restarts, where the node's latest known block
is from the latest run, and is not up-to-date, this block may be pruned as
well.
"""
from dataclasses import dataclass

from eth_utils import to_checksum_address, to_hex

from raiden.blockchain.events import DecodedEvent
from raiden.network.blockchain_service import BlockChainService
from raiden.network.proxies.utils import get_onchain_locksroots
from raiden.storage.restore import (
    get_event_with_balance_proof_by_locksroot,
    get_state_change_with_balance_proof_by_locksroot,
)
from raiden.storage.sqlite import SerializedSQLiteStorage
from raiden.transfer import views
from raiden.transfer.identifiers import CanonicalIdentifier
from raiden.transfer.state import ChainState, NettingChannelState
from raiden.utils.typing import (
    Address,
    Balance,
    BlockNumber,
    ChainID,
    Locksroot,
    Optional,
    PaymentNetworkAddress,
    TokenAddress,
    TokenNetworkAddress,
)


@dataclass(frozen=True)
class ChannelSettleState:
    """Recovered channel state that corresponds to the on-chain data."""

    canonical_identifier: CanonicalIdentifier
    our_locksroot: Locksroot
    partner_locksroot: Locksroot


@dataclass(frozen=True)
class NewChannelDetails:
    chain_id: ChainID
    payment_network_address: PaymentNetworkAddress
    token_address: TokenAddress
    our_address: Address
    our_initial_balance: Balance
    partner_address: Address
    partner_initial_balance: Balance


def get_contractreceivechannelsettled_data_from_event(
    chain_service: BlockChainService,
    chain_state: ChainState,
    event: DecodedEvent,
    latest_confirmed_block: BlockNumber,
) -> Optional[ChannelSettleState]:
    data = event.event_data
    token_network_address = TokenNetworkAddress(event.originating_contract)
    channel_identifier = data["args"]["channel_identifier"]
    block_hash = data["block_hash"]

    canonical_identifier = CanonicalIdentifier(
        chain_identifier=chain_state.chain_id,
        token_network_address=token_network_address,
        channel_identifier=channel_identifier,
    )

    channel_state = views.get_channelstate_by_canonical_identifier(
        chain_state=chain_state, canonical_identifier=canonical_identifier
    )

    # This may happen for two reasons:
    # - This node is not a participant for the given channel (normal operation,
    #   the event should be ignored).
    # - Something went wrong in our code and the channel state was cleared
    #   before settle (a bug, this should raise an exception on development
    #   mode).
    # Because we cannot distinguish the two cases, assume the channel is not of
    # interest and ignore the event.
    if not channel_state:
        return None

    # Recover the locksroot from the blockchain to fix data races. Check
    # get_onchain_locksroots for details.
    try:
        # First try to query the unblinded state. This way the
        # ContractReceiveChannelSettled's locksroots will  match the values
        # provided during settle.
        our_locksroot, partner_locksroot = get_onchain_locksroots(
            chain=chain_service,
            canonical_identifier=channel_state.canonical_identifier,
            participant1=channel_state.our_state.address,
            participant2=channel_state.partner_state.address,
            block_identifier=block_hash,
        )
    except ValueError:
        # State pruning handling. The block which generate the
        # ChannelSettled event may have been pruned, because of this the
        # RPC call raised ValueError.
        #
        # The solution is to query the channel's state from the latest
        # *confirmed* block, this /may/ create a ContractReceiveChannelSettled
        # with the wrong locksroot (i.e. not the locksroot used during the call
        # to settle). However this is fine, because at this point the channel
        # is settled, it is known that the locksroot can not be reverted
        # without an unlock, and because the unlocks are fair it doesn't matter
        # who called it, only if there are tokens locked in the settled
        # channel.
        our_locksroot, partner_locksroot = get_onchain_locksroots(
            chain=chain_service,
            canonical_identifier=channel_state.canonical_identifier,
            participant1=channel_state.our_state.address,
            participant2=channel_state.partner_state.address,
            block_identifier=latest_confirmed_block,
        )

    return ChannelSettleState(canonical_identifier, our_locksroot, partner_locksroot)


def get_contractreceiveupdatetransfer_data_from_event(
    chain_state: ChainState, event: DecodedEvent
) -> Optional[NettingChannelState]:
    data = event.event_data
    args = data["args"]
    channel_identifier = args["channel_identifier"]
    channel_state = views.get_channelstate_by_canonical_identifier(
        chain_state=chain_state,
        canonical_identifier=CanonicalIdentifier(
            chain_identifier=chain_state.chain_id,
            token_network_address=TokenNetworkAddress(event.originating_contract),
            channel_identifier=channel_identifier,
        ),
    )
    return channel_state


def get_contractreceivechannelbatchunlock_data_from_event(
    chain_state: ChainState, storage: SerializedSQLiteStorage, event: DecodedEvent
) -> Optional[CanonicalIdentifier]:
    token_network_address = TokenNetworkAddress(event.originating_contract)
    data = event.event_data
    args = data["args"]
    participant1 = args["receiver"]
    participant2 = args["sender"]
    locksroot = args["locksroot"]

    token_network_state = views.get_token_network_by_address(chain_state, token_network_address)
    assert token_network_state is not None

    if participant1 == chain_state.our_address:
        partner = participant2
    elif participant2 == chain_state.our_address:
        partner = participant1
    else:
        return None

    channel_identifiers = token_network_state.partneraddresses_to_channelidentifiers[partner]
    canonical_identifier = None

    for channel_identifier in channel_identifiers:
        if partner == args["sender"]:
            state_change_record = get_state_change_with_balance_proof_by_locksroot(
                storage=storage,
                canonical_identifier=CanonicalIdentifier(
                    chain_identifier=chain_state.chain_id,
                    token_network_address=token_network_address,
                    channel_identifier=channel_identifier,
                ),
                locksroot=locksroot,
                sender=partner,
            )
            if state_change_record is not None:
                canonical_identifier = (
                    state_change_record.data.balance_proof.canonical_identifier  # type: ignore
                )
                break
        elif partner == args["receiver"]:
            event_record = get_event_with_balance_proof_by_locksroot(
                storage=storage,
                canonical_identifier=CanonicalIdentifier(
                    chain_identifier=chain_state.chain_id,
                    token_network_address=token_network_address,
                    channel_identifier=channel_identifier,
                ),
                locksroot=locksroot,
                recipient=partner,
            )
            if event_record is not None:
                canonical_identifier = (
                    event_record.data.balance_proof.canonical_identifier  # type: ignore
                )
                break

    msg = (
        f"Can not resolve channel_id for unlock with locksroot {to_hex(locksroot)} and "
        f"partner {to_checksum_address(partner)}."
    )
    assert canonical_identifier is not None, msg

    return canonical_identifier


def get_contractreceivechannelnew_data_from_event(
    chain_state: ChainState,
    chain_service: BlockChainService,
    event: DecodedEvent,
    latest_confirmed_block: BlockNumber,
) -> Optional[NewChannelDetails]:
    token_network_address = TokenNetworkAddress(event.originating_contract)
    data = event.event_data
    args = data["args"]
    block_hash = data["block_hash"]
    participant1 = args["participant1"]
    participant2 = args["participant2"]

    is_participant = chain_state.our_address in (participant1, participant2)
    if not is_participant:
        return None

    channel_proxy = chain_service.payment_channel(
        canonical_identifier=CanonicalIdentifier(
            chain_identifier=event.chain_id,
            token_network_address=token_network_address,
            channel_identifier=args["channel_identifier"],
        )
    )
    try:
        channel_details = channel_proxy.detail(block_identifier=block_hash)
    except ValueError:
        # State pruning handling. The block which generate the ChannelNew
        # event may have been pruned, because of this the RPC call raised
        # ValueError.
        #
        # To fix this the latest **confirmed** state of the channel is queried.
        channel_details = channel_proxy.detail(block_identifier=latest_confirmed_block)

    token_network_registry = views.get_token_network_registry_by_token_network_address(
        chain_state, token_network_address
    )
    assert token_network_registry is not None, "Token network missing"

    our_details = channel_details.participants_data.our_details
    partner_details = channel_details.participants_data.partner_details

    return NewChannelDetails(
        chain_id=event.chain_id,
        payment_network_address=token_network_registry.address,
        token_address=channel_details.token_address,
        our_address=our_details.address,
        our_initial_balance=Balance(our_details.deposit),
        partner_address=partner_details.address,
        partner_initial_balance=Balance(partner_details.deposit),
    )


def get_contractreceivechannelclosed_data_from_event(
    chain_state: "ChainState", event: DecodedEvent
) -> Optional[CanonicalIdentifier]:
    token_network_address = TokenNetworkAddress(event.originating_contract)
    data = event.event_data
    args = data["args"]
    channel_identifier = args["channel_identifier"]

    channel_state = views.get_channelstate_by_canonical_identifier(
        chain_state=chain_state,
        canonical_identifier=CanonicalIdentifier(
            chain_identifier=chain_state.chain_id,
            token_network_address=token_network_address,
            channel_identifier=channel_identifier,
        ),
    )

    if channel_state:
        return channel_state.canonical_identifier

    return None
