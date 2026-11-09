from unittest.mock import Mock, patch

from django.test import TestCase

from stellar_sdk import StrKey

from aqua_marketkeys_tracker.marketkeys.loaders.asset_registry import AssetRegistryLoader
from aqua_marketkeys_tracker.marketkeys.models import Asset


LOADER_MODULE = 'aqua_marketkeys_tracker.marketkeys.loaders.asset_registry'

# Synthetic-but-valid strkeys: the loader checks the checksum, so a hand-written
# contract id would be rejected for the wrong reason.
NEW_CONTRACT = StrKey.encode_contract(bytes.fromhex('5d' * 32))
STALE_CONTRACT = StrKey.encode_contract(bytes.fromhex('7a' * 32))
ACCOUNT_ADDRESS = StrKey.encode_ed25519_public_key(bytes.fromhex('3c' * 32))

WHITELISTED_RECORD = {'asset_contract_address': NEW_CONTRACT, 'whitelisted': True}


class RegistryRecordValidationTestCase(TestCase):
    """Only whitelisted records feed the desired set, so only they need a valid address."""

    def setUp(self):
        # One asset the sync must flag and one it must clear, so that a completed
        # sync and an aborted one leave different state behind.
        Asset.objects.create(code='', issuer='', contract_id=NEW_CONTRACT, is_sac=False)
        Asset.objects.create(code='', issuer='', contract_id=STALE_CONTRACT, is_sac=False, in_asset_registry=True)

    def _sync(self, *records) -> None:
        response = Mock(ok=True)
        response.json.return_value = {'results': list(records), 'next': None}
        with patch(f'{LOADER_MODULE}.requests.get', return_value=response):
            AssetRegistryLoader().run()

    def _flagged_contracts(self) -> set:
        return set(Asset.objects.filter(in_asset_registry=True).values_list('contract_id', flat=True))

    def _assert_sync_completes(self, *records) -> None:
        self._sync(*records)

        self.assertEqual(self._flagged_contracts(), {NEW_CONTRACT})

    def _assert_sync_aborts(self, *records) -> None:
        with self.assertLogs(LOADER_MODULE, level='WARNING') as logs:
            self._sync(*records)

        self.assertIn('Asset registry record has unexpected shape', logs.output[0])
        self.assertEqual(self._flagged_contracts(), {STALE_CONTRACT})

    def test_non_whitelisted_account_address_is_ignored(self):
        """An account address is not a contract, but this record never reaches the desired set."""
        self._assert_sync_completes(
            {'asset_contract_address': ACCOUNT_ADDRESS, 'whitelisted': False},
            WHITELISTED_RECORD,
        )

    def test_non_whitelisted_record_without_address_is_ignored(self):
        self._assert_sync_completes({'whitelisted': False}, WHITELISTED_RECORD)

    def test_non_whitelisted_record_with_null_address_is_ignored(self):
        self._assert_sync_completes({'asset_contract_address': None, 'whitelisted': False}, WHITELISTED_RECORD)

    def test_whitelisted_record_with_invalid_address_aborts_the_sync(self):
        """These records drive additions and removals, so a bad one preserves local state."""
        for address in (ACCOUNT_ADDRESS, 'not-a-strkey', None):
            with self.subTest(address=address):
                self._assert_sync_aborts(
                    WHITELISTED_RECORD,
                    {'asset_contract_address': address, 'whitelisted': True},
                )

    def test_whitelisted_record_without_address_aborts_the_sync(self):
        self._assert_sync_aborts(WHITELISTED_RECORD, {'whitelisted': True})

    def test_record_without_a_boolean_whitelisted_flag_aborts_the_sync(self):
        """Without a usable flag the record cannot be classified, so the response is not trusted."""
        for malformed in (
            'not-a-dict',
            {'asset_contract_address': NEW_CONTRACT},
            {'asset_contract_address': NEW_CONTRACT, 'whitelisted': 'true'},
            {'asset_contract_address': NEW_CONTRACT, 'whitelisted': None},
        ):
            with self.subTest(record=malformed):
                self._assert_sync_aborts(WHITELISTED_RECORD, malformed)
