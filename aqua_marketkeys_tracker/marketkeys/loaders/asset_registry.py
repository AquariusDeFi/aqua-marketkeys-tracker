import json
import logging
from urllib.parse import urlparse

from django.conf import settings

import requests
from stellar_sdk import StrKey

from aqua_marketkeys_tracker.marketkeys.models import Asset


logger = logging.getLogger(__name__)


class AssetRegistryLoader:
    MAX_PAGES = 50

    def _is_valid_next(self, raw_next, expected_base):
        if not isinstance(raw_next, str):
            return False
        parsed = urlparse(raw_next)
        base = urlparse(expected_base)
        if parsed.scheme != "https":
            return False
        if parsed.netloc != base.netloc:
            return False
        # Reject path-traversal escapes before normalization.
        if ".." in parsed.path.split("/"):
            return False
        # Exact path match (after stripping trailing slash on both sides).
        # DRF pagination's `next` URL preserves the original endpoint path verbatim,
        # so an exact match is correct and rejects all sibling-endpoint escapes.
        return parsed.path.rstrip("/") == base.path.rstrip("/")

    def run(self):
        url = settings.GOVERNANCE_API_URL + settings.ASSET_REGISTRY_ENDPOINT
        all_results = []
        first_page = True
        next_url = url
        pages_fetched = 0

        while next_url:
            pages_fetched += 1
            if pages_fetched > self.MAX_PAGES:
                logger.warning(
                    "Asset registry sync aborted: pagination exceeded MAX_PAGES=%d (suspected upstream loop)",
                    self.MAX_PAGES,
                )
                return

            try:
                if first_page:
                    response = requests.get(
                        next_url,
                        params={"whitelisted": "true"},
                        timeout=30,
                        allow_redirects=False,
                    )
                    first_page = False
                else:
                    response = requests.get(next_url, timeout=30, allow_redirects=False)
            except requests.RequestException:
                logger.warning("Asset registry fetch failed.", exc_info=True)
                return

            if not response.ok:
                logger.warning(
                    "Asset registry returned non-2xx status: %s", response.status_code
                )
                return

            try:
                data = response.json()
            except json.JSONDecodeError:
                logger.warning("Asset registry response is not valid JSON.")
                return

            if (
                not isinstance(data, dict)
                or "results" not in data
                or not isinstance(data["results"], list)
            ):
                logger.warning("Asset registry response has unexpected shape.")
                return

            # Matching is contract-based: the registry serves the canonical
            # `asset_contract_address` per whitelisted token (SAC for classic assets,
            # the token contract for soroban ones), and every local Asset row
            # carries `contract_id`. `asset_code`/`asset_issuer` are nullable
            # for soroban tokens and are not used here.
            # Only whitelisted records feed the desired set, so only they need a
            # valid address; the address of any other record is not looked at.
            for record in data["results"]:
                if (
                    not isinstance(record, dict)
                    or "whitelisted" not in record
                    or not isinstance(record["whitelisted"], bool)
                    or (
                        record["whitelisted"]
                        and (
                            "asset_contract_address" not in record
                            or not isinstance(record["asset_contract_address"], str)
                            or not StrKey.is_valid_contract(record["asset_contract_address"])
                        )
                    )
                ):
                    logger.warning(
                        "Asset registry record has unexpected shape: %s", record
                    )
                    return

            all_results.extend(data["results"])
            raw_next = data.get("next")
            if raw_next is None:
                next_url = None
            elif self._is_valid_next(
                raw_next,
                settings.GOVERNANCE_API_URL + settings.ASSET_REGISTRY_ENDPOINT,
            ):
                next_url = raw_next
            else:
                logger.warning(
                    "Asset registry next URL rejected (off-host or malformed): %s",
                    raw_next,
                )
                return

        desired_set = {
            r["asset_contract_address"]
            for r in all_results
            if r["whitelisted"] is True
        }

        if not desired_set:
            logger.warning(
                "Asset registry desired set is empty after filtering; aborting to preserve local state."
            )
            return

        added = (
            Asset.objects.filter(in_asset_registry=False)
            .filter(contract_id__in=desired_set)
            .update(in_asset_registry=True)
        )
        removed = (
            Asset.objects.filter(in_asset_registry=True)
            .exclude(contract_id__in=desired_set)
            .update(in_asset_registry=False)
        )

        logger.info(
            f"added={added} removed={removed} desired_set_size={len(desired_set)}"
        )
