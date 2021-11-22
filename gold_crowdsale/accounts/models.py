from bip32utils import BIP32Key
from eth_keys import keys

from django.db import models

from gold_crowdsale.settings import ROOT_KEYS


class BlockchainAccount(models.Model):

    class Status(models.TextChoices):
        AVAILABLE = 'available'
        RECEIVING = 'receiving'

    eth_address = models.CharField(max_length=50, unique=True)
    btc_address = models.CharField(max_length=50, unique=True)

    status = models.CharField(max_length=50, choices=Status.choices, default=Status.AVAILABLE)

    last_updated = models.DateTimeField(auto_now=True)

    def generate_keys(self):
        root_public_key = ROOT_KEYS.get('public')
        bip32_key = BIP32Key.fromExtendedKey(root_public_key, public=True)

        child_key = bip32_key.ChildKey(self.id)

        self.eth_address = child_key.Address()
        self.btc_address = keys.PublicKey(child_key.K.to_string()).to_checksum_address().lower()

        self.save()

    def set_available(self):
        self.status = self.Status.AVAILABLE
        self.save()

    def set_receiving(self):
        self.status = self.Status.RECEIVING
        self.save()


