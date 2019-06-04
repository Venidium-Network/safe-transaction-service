from celery import app
from celery.utils.log import get_task_logger
from django.conf import settings
from eth_abi import decoding

from gnosis.eth import EthereumClientProvider
from gnosis.safe import Safe

from .models import MultisigConfirmation

logger = get_task_logger(__name__)


COUNTDOWN = 60  # seconds


def read_data_from_stream(self, stream):
    data = stream.read(self.data_byte_size)
    return data

# Workaround to fix InsufficientDataBytes exception raised by a wrong value format returned by
decoding.Fixed32ByteSizeDecoder.read_data_from_stream = read_data_from_stream


@app.shared_task(bind=True)
def check_approve_transaction(self, safe_address: str, contract_transaction_hash: str,
                              transaction_hash: str, owner: str, retry: bool=True) -> None:
    safe_reorg_blocks = settings.SAFE_REORG_BLOCKS
    ethereum_client = EthereumClientProvider()
    safe = Safe(safe_address, ethereum_client)

    current_block_number = ethereum_client.current_block_number
    block_identifier = current_block_number - settings.SAFE_REORG_BLOCKS
    try:
        multisig_confirmation = MultisigConfirmation.objects \
            .select_related('multisig_transaction').get(contract_transaction_hash=contract_transaction_hash,
                                                        owner=owner,
                                                        transaction_hash=transaction_hash)

        multisig_transaction = multisig_confirmation.multisig_transaction

        assert safe_address == multisig_transaction.safe

        is_executed_latest = multisig_transaction.nonce < safe.retrieve_nonce(block_identifier='latest')

        # If tx is executed hash in `approvedHashes` will be deleted to free storage and use gas for tx
        is_approved_latest = safe.retrieve_is_hash_approved(multisig_confirmation.owner,
                                                            contract_transaction_hash,
                                                            block_identifier='latest')

        is_approved_prev = safe.retrieve_is_hash_approved(multisig_confirmation.owner,
                                                          contract_transaction_hash,
                                                          block_identifier=block_identifier)

        transaction_data = ethereum_client.get_transaction(transaction_hash)

        if transaction_data:
            tx_block_number = transaction_data['blockNumber']
            if transaction_data['blockNumber'] != multisig_confirmation.block_number:
                if is_approved_prev and not is_approved_latest and not is_executed_latest:
                    # Detected reorg, multisig transaction not executed, also confirmation
                    # not approved either before and after blocks check
                    multisig_confirmation.delete()
                    return
                else:
                    # Update block number of stored confirmation
                    multisig_confirmation.block_number = tx_block_number
                    multisig_confirmation.save()

            if is_executed_latest:
                if not multisig_transaction.mined:
                    multisig_transaction.set_mined()
                return
            elif is_approved_latest:
                multisig_confirmation.set_mined()
                return

        else:  # Not transaction_data:
            # Check if more then X blocks have passed from the block number the transaction was created in DB
            if current_block_number - multisig_confirmation.block_number > safe_reorg_blocks:
                # Detected reorg, delete confirmation
                multisig_confirmation.delete()
                return

        if retry:
            self.retry(countdown=COUNTDOWN)

    except MultisigConfirmation.DoesNotExist:
        logger.warning('Multisig confirmation for safe=%s and transaction_hash=%s does not exist',
                       safe_address,
                       transaction_hash)
        return