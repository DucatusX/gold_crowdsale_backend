import logging
import sys
import traceback
from datetime import timedelta

from django.db import models
from django.utils import timezone
from eth_account import Account
from web3.exceptions import TransactionNotFound

from .utils import get_private_keys, normalize_gas_price
from gold_crowdsale.crypto_api.btc import BitcoinAPI, BitcoinRPC
from gold_crowdsale.crypto_api.eth import load_w3, load_eth_erc20_token

from gold_crowdsale.purchases.models import TokenPurchase
from gold_crowdsale.rates.models import get_rate_object
from gold_crowdsale.settings import MAX_AMOUNT_LEN, ROOT_KEYS, NETWORKS, DECIMALS


ERC20_CURRENCIES = ['USDT', 'USDC']
NATIVE_CURRENCIES = ['ETH', 'BTC']
AVAILABLE_CURRENCIES = NATIVE_CURRENCIES + ERC20_CURRENCIES


def create_withdraw_cycle(currencies=None):

    if not currencies:
        currencies = AVAILABLE_CURRENCIES
    else:
        if not isinstance(currencies, list):
            logging.error('Currencies must be list')
            return
        currencies = list(set(currencies))
        for currency in currencies:
            if currency not in AVAILABLE_CURRENCIES:
                logging.warning(f'Skipping currency {currency}: is not in supported list ({AVAILABLE_CURRENCIES})')
                currencies.pop(currency)

    currencies.sort()
    cycle = WithdrawCycle.objects.create(
        currencies=str(currencies),
        status=WithdrawCycle.Status.CREATED,
    )
    cycle.save()

    all_purchases = TokenPurchase.objects.all()

    for account in all_purchases:
        for currency in currencies:
            withdraw_initial_status = WithdrawTransaction.Status.CREATED

            if currency == 'ETH':
                erc20_txes = [x for x in currencies if x not in NATIVE_CURRENCIES]
                if len(erc20_txes) == 0:
                    continue

                withdraw_initial_status = WithdrawTransaction.Status.WAITING_FOR_ERC20_TRANSFERS

            elif currency in ERC20_CURRENCIES:
                gas_tx, created = WithdrawTransaction.objects.get_or_create(
                    withdraw_cycle=cycle,
                    account=account,
                    currency='ETH',
                    gas_tx_count=1,
                    tx_type=WithdrawTransaction.TransactionType.GAS_REFILL
                )
                gas_tx.save()

                if not created:
                    gas_tx.gas_tx_count += 1
                    gas_tx.save()

                withdraw_initial_status = WithdrawTransaction.Status.WAITING_FOR_GAS_REFILL

                # Create account queue for ERC20 withdrawals
                TransactionManager.objects.get_or_create(
                    withdraw_cycle=cycle,
                    account=account,
                    queue_type=TransactionManager.QueueType.ERC20
                )

                # Create global queue for gas refills
                TransactionManager.objects.get_or_create(
                    withdraw_cycle=cycle,
                    queue_type=TransactionManager.QueueType.GAS_REFILL
                )

            if currency in NATIVE_CURRENCIES:
                main_tx_type = WithdrawTransaction.TransactionType.NATIVE
            else:
                main_tx_type = WithdrawTransaction.TransactionType.ERC20

            WithdrawTransaction.objects.create(
                withdraw_cycle=cycle,
                account=account,
                currency=currency,
                status=withdraw_initial_status,
                tx_type=main_tx_type
            )

    cycle.status = WithdrawCycle.Status.PENDING
    cycle.save()


class WithdrawCycle(models.Model):
    class Status(models.TextChoices):
        CREATED = 'created'
        PENDING = 'pending'
        COMPLETED = 'completed'

    created_at = models.DateTimeField(auto_now_add=True)
    currencies = models.CharField(max_length=120, null=True, default=None)
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.CREATED)

    def check_for_completion(self):
        ongoing_tx_statuses = [
            WithdrawTransaction.Status.CREATED,
            WithdrawTransaction.Status.PENDING,
            WithdrawTransaction.Status.WAITING_FOR_ERC20_TRANSFERS,
            WithdrawTransaction.Status.WAITING_FOR_GAS_REFILL
        ]
        transactions = self.withdrawtransaction_set.filter(status__in=ongoing_tx_statuses)

        if transactions.count() > 0:
            return
        else:
            logging.info(f'Withdraw cycle {self.id} is finished')
            self.status = self.Status.COMPLETED
            self.save()


class WithdrawTransaction(models.Model):

    class Status(models.TextChoices):
        CREATED = 'created'
        PENDING = 'pending'
        WAITING_FOR_ERC20_TRANSFERS = 'waiting_for_erc20_transfers'
        WAITING_FOR_GAS_REFILL = 'waiting_for_gas_refill'
        COMPLETED = 'completed'
        SKIPPED = 'skipped'
        FAILED = 'failed'
        SENT_TX_NOT_FOUND = 'sent_tx_not_found'
        ERC20_BALANCE_TOO_LOW = 'erc20_balance_too_low'

    class TransactionType(models.TextChoices):
        NATIVE = 'native'
        ERC20 = 'erc20'
        GAS_REFILL = 'gas_refill'

    withdraw_cycle = models.ForeignKey(WithdrawCycle, on_delete=models.CASCADE, null=True, default=None)
    account = models.ForeignKey(TokenPurchase, on_delete=models.CASCADE, null=True, default=None)
    amount = models.DecimalField(max_digits=MAX_AMOUNT_LEN, decimal_places=0, null=True, default=None)
    currency = models.CharField(max_length=50)
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.CREATED)
    tx_type = models.CharField(max_length=50, choices=TransactionType.choices, default=TransactionType.NATIVE)
    tx_hash = models.CharField(max_length=100, null=True)
    gas_tx_count = models.IntegerField(null=True, default=None)
    gas_price_erc20 = models.DecimalField(max_digits=MAX_AMOUNT_LEN, decimal_places=0, null=True, default=None)
    error_message = models.TextField(default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    relayed_at = models.DateTimeField(null=True, default=None)

    def process_selector(self):
        if self.tx_type == self.TransactionType.GAS_REFILL:
            self.process_gas_refill()
        elif self.tx_type == self.TransactionType.ERC20:
            self.process_withdraw_erc20()
        else:
            if self.currency == 'BTC':
                self.process_withdraw_btc()
            elif self.currency == 'ETH':
                self.process_withdraw_eth()
            else:
                logging.error('Withdraw processing error: cannot select exact method for withdraw')
                return

    def process_withdraw_btc(self):
        if self.currency != 'BTC' and self.tx_type != self.TransactionType.NATIVE:
            logging.error(f'BTC processing called on tx with currency {self.currency} and type {self.tx_type}')
            return

        if self.status != self.Status.CREATED:
            logging.error('Already processed or processing in progress')
            return

        from_address = self.account.btc_address
        logging.info(f'BTC address: {from_address}')

        _, priv_key = get_private_keys(ROOT_KEYS.get('private'), self.account.id)

        to_address = NETWORKS.get('BTC').get('withdraw_address')
        if not to_address:
            logging.info(f'Withdraw address is not set. Aborting')

        api = BitcoinAPI()
        inputs, value, response_ok = api.get_address_unspent_all(from_address)

        if not response_ok:
            logging.error(f'Failed to fetch information about BTC address {from_address}')
            return

        balance = int(value)
        if balance <= 0:
            balance = 0

        rpc = BitcoinRPC()
        transaction_fee = rpc.relay_fee
        if balance < transaction_fee:
            logging.info(f'Address skipped: {from_address}: balance {balance} < tx fee of {transaction_fee}')
            self.status = self.Status.SKIPPED
            self.save()
            return

        withdraw_amount = (balance - transaction_fee) / DECIMALS['BTC']

        output_params = {to_address: withdraw_amount}

        logging.info(f'Withdraw tx params: from {from_address} to {to_address} on amount {withdraw_amount}')
        logging.info(f'input_params: {inputs}')
        logging.info(f'output_params: {output_params}')

        tx_hash_or_error_msg, send_success = rpc.construct_and_send_tx(inputs, output_params, priv_key)

        if send_success:
            self.tx_hash = tx_hash_or_error_msg
            self.status = self.Status.PENDING
            self.relayed_at = timezone.now()
            self.save()
            logging.info(f'Withdraw BTC tx sent: {self.tx_hash}')
        else:
            self.status = self.Status.FAILED
            self.error_message = tx_hash_or_error_msg
            self.save()
            err_str = f'Withdraw failed for address {from_address} and ' \
                      f'amount {withdraw_amount} ({balance} - {transaction_fee})'
            logging.error(err_str)

    def process_gas_refill(self):
        if self.currency != 'ETH' and self.tx_type != self.TransactionType.GAS_REFILL:
            logging.error(f'Gas refill processing called on tx with currency {self.currency} and type {self.tx_type}')
            return

        if self.status != self.Status.CREATED:
            logging.error(f'WITHDRAWAL: id {self.id} already processed or processing in progress {self.status}')
            return

        refill_queue_manager_set = self.transactionmanager_set.filter(
            withdraw_cycle=self.withdraw_cycle,
            queue_type=self.tx_type,
            tx_to_process_id=self.id
        )
        if not refill_queue_manager_set:
            logging.info(f'TRANSFER: Transfer with id {self.id} postponed due multiple txes in queue')
            return

        web3 = load_w3('ETH')
        gas_price, fake_gas_price = normalize_gas_price(web3.eth.gasPrice)

        refill_gas_limit = 21000
        refill_gas_fee = gas_price * refill_gas_limit

        base_erc20_gas_limit = 200000
        erc20_gas_limit = base_erc20_gas_limit * self.gas_tx_count
        erc20_gas_fee = gas_price * erc20_gas_limit

        address_to_refill = self. account.eth_address

        rate_object = get_rate_object()
        eth_rate = rate_object.ETH
        total_erc20_balances_in_eth = 0

        erc20_withdrawals = WithdrawTransaction.objects.filter(
            withdraw_cycle=self.withdraw_cycle,
            account=self.account,
            tx_type=WithdrawTransaction.TransactionType.ERC20
        )

        for token_withdrawal in erc20_withdrawals:
            _, token_contract = load_eth_erc20_token(token_withdrawal.currency)
            token_balance = token_contract.functions.balanceOf(web3.toChecksumAddress(address_to_refill)).call()
            token_rate = getattr(rate_object, token_withdrawal.currency)
            token_balance_in_eth = int(
                ((float(token_balance) / float(DECIMALS[token_withdrawal.currency]))
                 * float(token_rate) * float(eth_rate)) * float(DECIMALS['ETH'])
            )
            total_erc20_balances_in_eth += token_balance_in_eth
            token_withdrawal.amount = token_balance
            token_withdrawal.gas_price_erc20 = gas_price
            token_withdrawal.save()

        total_gas_refill_cost = refill_gas_fee + erc20_gas_fee
        if total_erc20_balances_in_eth <= total_gas_refill_cost:
            logging.info(f'Refill on address {address_to_refill} skipped: '
                         f'tokens value in ETH {total_erc20_balances_in_eth}  < tx fee of {total_gas_refill_cost}')
            self.status = self.Status.ERC20_BALANCE_TOO_LOW
            self.save()

            for token_withdrawal in erc20_withdrawals:
                token_withdrawal.status = self.Status.SKIPPED
                token_withdrawal.save()

            return

        address_to_refill_balance = web3.eth.getBalance(web3.toChecksumAddress(address_to_refill))

        if address_to_refill_balance > int(erc20_gas_fee * 1.1):
            logging.info(f'Refill on address {address_to_refill} skipped: '
                         f'Current address balance {address_to_refill_balance} > {int(erc20_gas_fee * 1.1)} '
                         f'and is enough for withdrawing ERC20 tokens')
            self.status = self.Status.SKIPPED
            self.save()
            return

        address_with_gas = NETWORKS.get('ETH').get('gas_address')
        gas_nonce = web3.eth.getTransactionCount(web3.toChecksumAddress(address_with_gas), 'pending')

        refill_amount = int(erc20_gas_fee * 1.2)
        self.amount = refill_amount
        self.save()

        gas_tx_params = {
            'chainId': web3.eth.chainId,
            'gas': refill_gas_limit,
            'nonce': gas_nonce,
            'gasPrice': fake_gas_price,
            'to': web3.toChecksumAddress(address_to_refill),
            'value': refill_amount
        }

        priv_key = NETWORKS.get('ETH').get('gas_privkey')
        signed_tx = Account.signTransaction(gas_tx_params, priv_key)

        try:
            sent_tx = web3.eth.sendRawTransaction(signed_tx['rawTransaction'])
            self.status = self.Status.PENDING
            self.tx_hash = sent_tx.hex()
            self.gas_price_erc20 = gas_price
            self.relayed_at = timezone.now()
            self.save()
            logging.info(f'Refill tx sent: {self.tx_hash}')
        except Exception as e:
            self.status = self.Status.FAILED
            self.error_message = e
            self.save()
            logging.error(f'Refill failed for address {address_to_refill} and amount {refill_amount}), error: {e}')
            logging.error('\n'.join(traceback.format_exception(*sys.exc_info())))

    def process_withdraw_erc20(self):
        if self.currency not in ['USDC', 'USDT'] and self.tx_type != self.TransactionType.ERC20:
            logging.error(f'ERC20 processing called on tx with currency {self.currency} and type {self.tx_type}')
            return

        erc20_processing_statuses = [
            self.Status.CREATED,
            self.Status.WAITING_FOR_GAS_REFILL,
            self.Status.WAITING_FOR_ERC20_TRANSFERS
        ]

        if self.status not in erc20_processing_statuses:
            logging.error(f'WITHDRAWAL: id {self.id} already processed or processing in progress {self.status}')
            return

        refill_transaction = WithdrawTransaction.objects.filter(
            withdraw_cycle=self.withdraw_cycle,
            account=self.account,
            tx_type=self.TransactionType.GAS_REFILL
        )

        if not refill_transaction:
            logging.warning('Refill transaction not found, skipping withdraw')
            self.status = self.Status.SKIPPED
            self.save()
            return
        else:
            refill_transaction = refill_transaction.get()

        if refill_transaction.status == self.Status.FAILED:
            logging.info(f'Withdraw {self.currency}: id {self.id} skipped - refill transaction failed')
            self.status = self.Status.SKIPPED
            self.save()
            return
        elif refill_transaction.status not in [self.Status.COMPLETED, self.Status.SKIPPED]:
            logging.info(f'WITHDRAW {self.currency}: id {self.id} postponed - Refill transaction is not completed yet')
            return

        erc20_queue_manager_set = self.transactionmanager_set.filter(
            withdraw_cycle=self.withdraw_cycle,
            queue_type=self.tx_type,
            account=self.account,
            tx_to_process_id=self.id
        )
        if self.status in erc20_processing_statuses[1:] and not erc20_queue_manager_set:
            logging.info(f'Withdraw {self.id} delayed because other ERC20 withdrawals in queue')

            if self.status == self.Status.WAITING_FOR_GAS_REFILL:
                self.status = self.Status.WAITING_FOR_ERC20_TRANSFERS
                self.save()

            return

        web3, token_contract = load_eth_erc20_token(self.currency)

        from_address = self.account.eth_address
        withdraw_amount = int(self.amount)

        priv_key, _ = get_private_keys(ROOT_KEYS.get('private'), self.account.id)

        to_address = NETWORKS.get('ETH').get('withdraw_address')
        gas_price, fake_gas_price = normalize_gas_price(self.gas_price_erc20)
        erc20_gas_limit = 200000
        nonce = web3.eth.getTransactionCount(web3.toChecksumAddress(from_address), 'pending')

        tx_params = {
            'chainId': web3.eth.chainId,
            'nonce': nonce,
            'gas': erc20_gas_limit,
            'gasPrice': fake_gas_price,
        }

        logging.info(f'Withdraw tx params: from {from_address} to {to_address} on amount {withdraw_amount}')
        initial_tx = token_contract.functions.transfer(to_address, withdraw_amount).buildTransaction(tx_params)
        signed_tx = Account.signTransaction(initial_tx, priv_key)
        try:
            sent_tx = web3.eth.sendRawTransaction(signed_tx['rawTransaction'])
            self.tx_hash = sent_tx.hex()
            self.status = self.Status.PENDING
            self.relayed_at = timezone.now()
            self.save()
            logging.info(f'Withdraw {self.currency} tx sent: {self.tx_hash}')
        except Exception as e:
            self.status = self.Status.FAILED
            self.error_message = e
            self.save()
            logging.error(f'Withdraw failed for address {from_address} and amount {withdraw_amount}, error is: {e}')
            logging.error('\n'.join(traceback.format_exception(*sys.exc_info())))

    def process_withdraw_eth(self):
        if self.currency != 'ETH' and self.tx_type != self.TransactionType.NATIVE:
            logging.error(f'WITHDRAWAL: ETH processing called on tx with '
                          f'currency {self.currency} and type {self.tx_type}')
            return
        if self.status not in [self.Status.CREATED, self.Status.WAITING_FOR_ERC20_TRANSFERS]:
            logging.error(f'WITHDRAWAL: id {self.id} already processed or processing in progress {self.status}')
            return
        if self.status == self.Status.WAITING_FOR_ERC20_TRANSFERS:
            # check erc20 tx statuses here
            all_erc20_withdrawals = WithdrawTransaction.objects.filter(
                withdraw_cycle=self.withdraw_cycle,
                account=self.account,
                tx_type=WithdrawTransaction.TransactionType.ERC20,
            )

            completed_erc20_txes = all_erc20_withdrawals.filter(
                status__in=[WithdrawTransaction.Status.SKIPPED, WithdrawTransaction.Status.COMPLETED]
            )
            if completed_erc20_txes.count() < all_erc20_withdrawals.count():
                logging.info('Delaying tx because waiting for ERC20 transfer')
                return

        priv_key, _ = get_private_keys(ROOT_KEYS.get('private'), self.account.id)
        logging.info(f'ETH address: {self.account.eth_address}')

        web3 = load_w3('ETH')

        gas_limit = 21000
        gas_price, fake_gas_price = normalize_gas_price(web3.eth.gasPrice)
        total_gas_fee = gas_price * gas_limit

        from_address = self.account.eth_address
        to_address = NETWORKS.get('ETH').get('withdraw_address')

        balance = web3.eth.getBalance(web3.toChecksumAddress(from_address))
        nonce = web3.eth.getTransactionCount(web3.toChecksumAddress(from_address), 'pending')

        if balance < total_gas_fee:
            logging.info(f'Address {from_address} skipped: balance {balance} < tx fee of {total_gas_fee}')
            self.status = self.Status.SKIPPED
            self.save()
            return

        withdraw_amount = int(balance) - total_gas_fee

        tx_params = {
            'chainId': web3.eth.chainId,
            'gas': gas_limit,
            'nonce': nonce,
            'gasPrice': fake_gas_price,
            'to': web3.toChecksumAddress(to_address),
            'value': int(withdraw_amount)
        }

        logging.info(f'Withdraw tx params: from {from_address} to {to_address} on amount {withdraw_amount}')

        signed_tx = Account.signTransaction(tx_params, priv_key)
        try:
            sent_tx = web3.eth.sendRawTransaction(signed_tx['rawTransaction'])
            self.status = self.Status.PENDING
            self.tx_hash = sent_tx.hex()
            self.relayed_at = timezone.now()
            self.save()
            logging.info(f'Withdraw ETH sent tx: {sent_tx.hex()}')
        except Exception as e:
            self.status = self.Status.FAILED
            self.error_message = e
            err_str = f'Withdraw ETH failed for address {from_address} ' \
                      f'and amount {withdraw_amount} ({balance} - {total_gas_fee})'
            logging.error(err_str)
            logging.error(e)
            logging.error('\n'.join(traceback.format_exception(*sys.exc_info())))

    def confirm_selector(self):
        if self.currency == 'ETH' or self.tx_type == self.TransactionType.ERC20:
            self.tx_confirm_eth_erc20()
        elif self.currency == 'BTC':
            self.tx_confirm_btc()
        else:
            logging.error('Withdraw processing error: cannot select exact method for withdraw')
            return

    def tx_confirm_eth_erc20(self):
        if self.status == self.Status.COMPLETED:
            logging.info(f'WITHDRAW CONFIRMATION: Token transfer already validated (tx: {self.tx_hash})')
            return

        web3 = load_w3('ETH')
        try:
            tx_receipt = web3.eth.getTransactionReceipt(self.tx_hash)
        except TransactionNotFound:
            self.check_confirmation_time(hours=2)
            return

        if tx_receipt.get('status') == 0:
            self.status = self.Status.FAILED
            self.error_message = 'reverted'
            logging.info(f'WITHDRAW CONFIRMATION: Withdraw {self.tx_hash} reverted')
        else:
            self.status = self.Status.COMPLETED
            logging.info(f'WITHDRAW CONFIRMATION: Withdraw {self.tx_hash} completed')

        self.save()

    def tx_confirm_btc(self):
        if self.status == self.Status.COMPLETED:
            logging.info(f'WITHDRAW CONFIRMATION: Token transfer already validated (tx: {self.tx_hash})')
            return

        api = BitcoinAPI()

        try:
            confirmations = api.get_tx_confirmations(self.tx_hash)
        except Exception as e:
            logging.info(f'Exception when trying to get BTC confirmations: {e}')
            logging.error('\n'.join(traceback.format_exception(*sys.exc_info())))
            self.check_confirmation_time(hours=3)
            return

        if confirmations > 3:
            logging.info(f'WITHDRAW CONFIRMATION: Withdraw {self.tx_hash} completed')
            self.status = self.Status.COMPLETED
            self.save()

    def check_confirmation_time(self, hours):
        if timezone.now() < self.relayed_at + timedelta(hours=hours):
            logging.info('Transaction not found, retrying')
        else:
            self.status = self.Status.SENT_TX_NOT_FOUND
            self.save()


class TransactionManager(models.Model):

    class QueueType(models.TextChoices):
        ERC20 = 'erc20'
        GAS_REFILL = 'gas_refill'

    withdraw_cycle = models.ForeignKey(WithdrawCycle, on_delete=models.CASCADE, null=True, default=None)
    account = models.ForeignKey(TokenPurchase, on_delete=models.CASCADE, null=True, default=None)
    tx_to_process = models.ForeignKey(WithdrawTransaction, on_delete=models.CASCADE, null=True, default=None)
    queue_type = models.CharField(max_length=50, choices=QueueType.choices, default=QueueType.ERC20)
    created_at = models.DateTimeField(auto_now_add=True)
    completed = models.BooleanField(default=False, null=True)

    def is_current_tx_finished(self):
        if not self.tx_to_process:
            return True

        return self.tx_to_process.status != WithdrawTransaction.Status.PENDING
        # withdrawals = WithdrawTransaction.objects.filter(
        #     withdraw_cycle=self.withdraw_cycle,
        #     tx_type=self.queue_type,
        #     status__in=[
        #         WithdrawTransaction.Status.PENDING,
        #     ]
        # )
        #
        # if self.queue_type == self.QueueType.ERC20:
        #     withdrawals = withdrawals.filter(account=self.account)
        #
        # return True if not withdrawals else False

    def get_remaining_txes(self):
        withdrawals = WithdrawTransaction.objects.filter(
            withdraw_cycle=self.withdraw_cycle,
            tx_type=self.queue_type,
        )
        if self.queue_type == self.QueueType.ERC20:
            withdrawals = withdrawals.filter(
                account=self.account,
                status__in=[
                    WithdrawTransaction.Status.CREATED,
                    WithdrawTransaction.Status.WAITING_FOR_ERC20_TRANSFERS,
                    WithdrawTransaction.Status.WAITING_FOR_GAS_REFILL
                ]
            ).order_by('currency')
        else:
            withdrawals = withdrawals.filter(status=WithdrawTransaction.Status.CREATED).order_by('account_id')

        return withdrawals

    def set_next_tx(self):
        if self.completed:
            logging.info(f'WITHDRAW QUEUE: Method to set next tx is called on id {self.id}, ignoring')
            return

        if not self.is_current_tx_finished():
            return

        remaining_withdrawals = self.get_remaining_txes()
        if remaining_withdrawals:
            self.tx_to_process = remaining_withdrawals.first()
        else:
            self.tx_to_process = None
            self.completed = True

        self.save()
