import structlog
from eth_utils import decode_hex, event_abi_to_log_topic, to_checksum_address
from gevent.lock import Semaphore
from web3 import Web3
from web3.utils.abi import filter_by_type
from web3.utils.events import get_event_data
from web3.utils.filters import LogFilter, construct_event_filter_params

from raiden.constants import GENESIS_BLOCK_NUMBER
from raiden.utils import block_specification_to_number
from raiden.utils.typing import (
    Any,
    BlockNumber,
    BlockSpecification,
    ChannelID,
    Dict,
    List,
    TokenNetworkAddress,
)
from raiden_contracts.constants import CONTRACT_TOKEN_NETWORK, ChannelEvent
from raiden_contracts.contract_manager import ContractManager

log = structlog.get_logger(__name__)  # pylint: disable=invalid-name

# The maximum block range to query in a single filter query
# Helps against timeout errors that occur if you query a filter for
# the mainnet from Genesis to latest head as we see in:
# https://github.com/raiden-network/raiden/issues/3558
FILTER_MAX_BLOCK_RANGE = 100000


def get_filter_args_for_specific_event_from_channel(
    token_network_address: TokenNetworkAddress,
    channel_identifier: ChannelID,
    event_name: str,
    contract_manager: ContractManager,
    from_block: BlockSpecification = GENESIS_BLOCK_NUMBER,
    to_block: BlockSpecification = "latest",
):
    """ Return the filter params for a specific event of a given channel. """
    if not event_name:
        raise ValueError("Event name must be given")

    event_abi = contract_manager.get_event_abi(CONTRACT_TOKEN_NETWORK, event_name)

    # Here the topics for a specific event are created
    # The first entry of the topics list is the event name, then the first parameter is encoded,
    # in the case of a token network, the first parameter is always the channel identifier
    _, event_filter_params = construct_event_filter_params(
        event_abi=event_abi,
        contract_address=to_checksum_address(token_network_address),
        argument_filters={"channel_identifier": channel_identifier},
        fromBlock=from_block,
        toBlock=to_block,
    )

    return event_filter_params


def get_filter_args_for_all_events_from_channel(
    token_network_address: TokenNetworkAddress,
    channel_identifier: ChannelID,
    contract_manager: ContractManager,
    from_block: BlockSpecification = GENESIS_BLOCK_NUMBER,
    to_block: BlockSpecification = "latest",
) -> Dict:
    """ Return the filter params for all events of a given channel. """

    event_filter_params = get_filter_args_for_specific_event_from_channel(
        token_network_address=token_network_address,
        channel_identifier=channel_identifier,
        event_name=ChannelEvent.OPENED,
        contract_manager=contract_manager,
        from_block=from_block,
        to_block=to_block,
    )

    # As we want to get all events for a certain channel we remove the event specific code here
    # and filter just for the channel identifier
    # We also have to remove the trailing topics to get all filters
    event_filter_params["topics"] = [None, event_filter_params["topics"][1]]

    return event_filter_params


def decode_event(abi: List[Dict], log: Dict):
    """ Helper function to unpack event data using a provided ABI

    Args:
        abi: The ABI of the contract, not the ABI of the event
        log: The raw event data

    Returns:
        The decoded event
    """
    if isinstance(log["topics"][0], str):
        log["topics"][0] = decode_hex(log["topics"][0])
    elif isinstance(log["topics"][0], int):
        log["topics"][0] = decode_hex(hex(log["topics"][0]))
    event_id = log["topics"][0]
    events = filter_by_type("event", abi)
    topic_to_event_abi = {event_abi_to_log_topic(event_abi): event_abi for event_abi in events}
    event_abi = topic_to_event_abi[event_id]
    return get_event_data(event_abi, log)


class StatelessFilter(LogFilter):
    """ Like LogFilter, but uses eth_getLogs instead of installed filter

    Pass latest block_number to get_(new|all)_entries to avoid querying it
    """

    def __init__(self, web3: Web3, filter_params: dict):
        super().__init__(web3, filter_id=None)
        self.filter_params: Dict[str, BlockSpecification] = filter_params
        self._last_block: BlockNumber = BlockNumber(-1)
        self._lock = Semaphore()

    def _do_get_new_entries(self, from_block: BlockSpecification, to_block: BlockSpecification):
        filter_params = self.filter_params.copy()
        filter_params["fromBlock"] = from_block
        filter_params["toBlock"] = to_block

        log.debug("Querying StatelessFilter", from_block=from_block, to_block=to_block)
        result = self.web3.eth.getLogs(filter_params)
        self._last_block = block_specification_to_number(block=to_block, web3=self.web3)
        return result

    def get_new_entries(self, target_block_number: BlockNumber) -> List[Dict[str, Any]]:
        with self._lock:
            result: List[Dict[str, Any]] = []
            filter_from_number = block_specification_to_number(
                block=self.filter_params.get("fromBlock", GENESIS_BLOCK_NUMBER), web3=self.web3
            )
            from_block_number = max(filter_from_number, self._last_block + 1)

            # Batch the filter queries in ranges of FILTER_MAX_BLOCK_RANGE
            # to avoid timeout problems
            while from_block_number <= target_block_number:
                to_block = min(from_block_number + FILTER_MAX_BLOCK_RANGE, target_block_number)
                result.extend(
                    self._do_get_new_entries(from_block=from_block_number, to_block=to_block)
                )
                from_block_number += FILTER_MAX_BLOCK_RANGE

            return result

    def get_all_entries(self, block_number: BlockNumber = None):
        with self._lock:
            filter_params = self.filter_params.copy()
            block_number = block_number or self.web3.eth.blockNumber

            if self.filter_params.get("toBlock") in ("latest", "pending"):
                filter_params["toBlock"] = block_number

            result = self.web3.eth.getLogs(filter_params)
            to_block = filter_params.get("toBlock")
            if to_block:
                self._last_block = block_specification_to_number(block=to_block, web3=self.web3)
            else:
                self._last_block = block_number

            return result
