"""Empty OMEMO messages are session setup, not undecryptable chat messages."""

from __future__ import annotations

import pytest
from xml.etree import ElementTree as ET

pytest.importorskip("omemo")

from ward.core import stanzas
from ward.core.omemo.service import OmemoService


def message(encrypted: str) -> stanzas.ParsedMessage:
    pm = stanzas.ParsedMessage()
    pm.omemo = ET.fromstring(encrypted) if "eu.siacs" in encrypted else None
    pm.omemo2 = ET.fromstring(encrypted) if "urn:xmpp:omemo:2" in encrypted else None
    return pm


def test_legacy_key_exchange_has_no_payload() -> None:
    pm = message("<encrypted xmlns='eu.siacs.conversations.axolotl'>"
                 "<header sid='1'><key rid='2'>abc</key><iv>xyz</iv></header></encrypted>")
    assert OmemoService._omemo_is_key_exchange(pm)


def test_legacy_empty_payload_is_a_key_exchange() -> None:
    pm = message("<encrypted xmlns='eu.siacs.conversations.axolotl'>"
                 "<header sid='1'><key rid='2'>abc</key><iv>xyz</iv></header><payload> </payload></encrypted>")
    assert OmemoService._omemo_is_key_exchange(pm)


def test_legacy_message_with_payload_is_not_a_key_exchange() -> None:
    pm = message("<encrypted xmlns='eu.siacs.conversations.axolotl'>"
                 "<header sid='1'><key rid='2'>abc</key><iv>xyz</iv></header><payload>YWJj</payload></encrypted>")
    assert not OmemoService._omemo_is_key_exchange(pm)


def test_omemo2_key_exchange_has_no_payload() -> None:
    pm = message("<encrypted xmlns='urn:xmpp:omemo:2'>"
                 "<header sid='1'><keys jid='us@example.org'><key rid='2'>abc</key></keys></header></encrypted>")
    assert OmemoService._omemo_is_key_exchange(pm)


def test_omemo2_message_with_payload_is_not_a_key_exchange() -> None:
    pm = message("<encrypted xmlns='urn:xmpp:omemo:2'>"
                 "<header sid='1'><keys jid='us@example.org'><key rid='2'>abc</key></keys></header>"
                 "<payload>YWJj</payload></encrypted>")
    assert not OmemoService._omemo_is_key_exchange(pm)
