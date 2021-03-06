"""
Based off of: https://github.com/CityOfZion/neo-python/blob/master/examples/smart-contract.py

Example of running a NEO node and receiving notifications when events
of a specific smart contract happen.

Events include Runtime.Notify, Runtime.Log, Storage.*, Execution.Success
and several more. See the documentation here:

http://neo-python.readthedocs.io/en/latest/smartcontracts.html

Usage:

* Update config/bulk-tx-config.json params
* Update the job config file (as defined in bulk-tx-config.json) to contain the jobs to process
* Update config/network-wallets.json wallet path for the selected network
* Place this file in neo-python/neo/contrib and execute the following from neo-python dir:

python3.5 -m venv venv
source venv/bin/activate
# bl: these only need to be done once
#pip install -U setuptools pip wheel
#pip install -e .
python neo/contrib/bulk-process-tx.py

"""
import os
import json
from time import sleep

from neo.Core.Blockchain import Blockchain

from neo.contrib.smartcontract import SmartContract

from neo.Prompt.Utils import parse_param
from neo.Prompt.Commands.Send import construct_and_send

from pyparsing import ZeroOrMore, Regex

from neo.contrib.narrative.blockchain.main import BlockchainMain, NetworkType


class BulkProcess(BlockchainMain):
    # from InputParser
    parser = ZeroOrMore(Regex(r'\[[^]]*\]') | Regex(r'"[^"]*"') | Regex(r'\'[^\']*\'') | Regex(r'[^ ]+'))

    smart_contract_hash = None

    operation = None
    operation_args_array_length = None
    expected_result_count = None
    from_addr = None

    test_only = False

    wallet_needs_recovery = False

    smart_contract = None

    job = None

    jobs = None

    jobs_processed = 0
    tx_processing = None

    def __init__(self):
        with open(os.path.join(os.path.abspath(os.path.dirname(__file__)), 'config', 'bulk-tx-config.json'), 'r') as f:
            config = json.load(f)
        with open(os.path.join(os.path.abspath(os.path.dirname(__file__)), 'config', config['job_config_file']), 'r') as f:
            job_config = json.load(f)
        with open(os.path.join(os.path.abspath(os.path.dirname(__file__)), 'config', 'network-wallets.json'), 'r') as f:
            network_wallets_config = json.load(f)

        super().__init__(NetworkType[config['network']], 'bulk-process-tx')

        self.test_only = config['test_only']

        self.operation = job_config['operation']
        self.operation_args_array_length = job_config['operation_args_array_length']
        self.expected_result_count = job_config['expected_result_count']
        try:
            self.from_addr = job_config['from_addr']
        except KeyError:
            pass

        self.jobs = job_config['jobs']

        # Setup the smart contract instance
        self.smart_contract_hash = config['smart_contract']
        self.smart_contract = SmartContract(self.smart_contract_hash)

        # decorate the event handler methods dynamically now that we have loaded the SC
        self.sc_notify = self.smart_contract.on_notify(self.sc_notify)
        self.sc_storage = self.smart_contract.on_storage(self.sc_storage)
        self.sc_execution = self.smart_contract.on_execution(self.sc_execution)

        self.setup_wallet(network_wallets_config[config['network']]['wallet_path'])

    def pre_start(self):
        # trigger the first job to be processed
        self.process_job()

    def sc_notify(self, event):
        if not event.execution_success:
            return

        prefix = ""
        if event.test_mode:
            prefix = "[test_mode]"
        elif event.tx_hash != self.tx_processing:
            # only emit notify events for the transaction that we are waiting on
            return

        self.logger.info(prefix + "[SmartContract.Runtime.Notify] [%s] [tx %s] %s", event.contract_hash, event.tx_hash, event.event_payload)

    def sc_storage(self, event):
        prefix = ""
        if event.test_mode:
            prefix = "[test_mode]"
        elif event.tx_hash != self.tx_processing:
            # only emit notify events for the transaction that we are waiting on
            return

        self.logger.info(prefix + "[%s] [%s] [tx %s] %s", event.event_type, event.contract_hash, event.tx_hash, event.event_payload)

    def sc_execution(self, event):
        # only emit execution events for the transaction that we are waiting on
        if event.tx_hash != self.tx_processing:
            return

        if not event.execution_success:
            self.logger.error("[execution_success=false][SmartContract.Runtime.Notify] [%s] [tx %s] %s", event.contract_hash, event.tx_hash, event.event_payload)
            return

        prefix = ""
        if event.test_mode:
            prefix = "[test_mode]"

        self.logger.info(prefix + "[SmartContract.Execution.Success] [%s] [tx %s] %s", event.contract_hash, event.tx_hash, event.event_payload)

        if not event.test_mode:
            self.jobs_processed += 1
            self.process_job()

    def process_job(self):
        jobs_remaining = len(self.jobs)

        self.logger.debug("%s jobs processed. %s jobs remaining.", self.jobs_processed, jobs_remaining)

        self.tx_processing = None

        if jobs_remaining > 0:
            # just pop a job off the array to process next
            self.job = self.jobs[0]
            self.jobs = self.jobs[1:]
        else:
            # change the jobs array to None (from an empty array) to indicate we are done and can shut down
            self.jobs = None

    def custom_background_code(self):
        """ Custom code run in a background thread. Prints the current block height.

        This function is run in a daemonized thread, which means it can be instantly killed at any
        moment, whenever the main thread quits. If you need more safety, don't use a  daemonized
        thread and handle exiting this thread in another way (eg. with signals and events).
        """
        while True:
            sleep(1)

            if not self.job:
                # no more jobs? then shut 'er down!
                if self.jobs is None:
                    self.shutdown()

                # if it's a refund job, then check to see if we have the transaction recorded yet. if not, keep waiting.
                # note that this will give an info log "Could not find transaction for hash b'xxx'" every second until the tx is processed.
                if self.is_refund_job() and self.tx_processing:
                    tx, height = Blockchain.Default().GetTransaction(self.tx_processing)
                    # the tx will have a height once it's completed!
                    if height > -1:
                        # the tx has been processed, so process the next refund!
                        self.jobs_processed += 1
                        self.process_job()
                continue

            if self.wallet_needs_recovery:
                self.recover_wallet()
                self.wallet_needs_recovery = False
            else:
                self.wallet_sync()

            # special handling for sending refunds
            if self.is_refund_job():
                self.process_refund_job()
            else:
                self.process_testinvoke_job()

    def is_refund_job(self):
        return self.operation == 'send'

    def process_refund_job(self):
        if len(self.job) != self.operation_args_array_length:
            self.logger.error('ERROR! must have exactly %d operation args, not %d. skipping! %s', self.operation_args_array_length, len(self.job), self.job)
            self.job = None
            self.process_job()
            return

        # bl: tx can fail if there are no connected peers, so wait for one
        self.wait_for_peers()

        self.logger.debug('processing refund: %s', self.job)
        # in case we have to rebuild the wallet and try the job again, pass in a new list to construct_and_send
        # since internally the method actually has a side effect of modifying the array to strip out the from address
        result = construct_and_send(None, self.wallet, list(self.job), False)

        if not result:
            self.wallet_needs_recovery = True
        else:
            self.job = None
            self.tx_processing = result.Hash

    def process_testinvoke_job(self):
        job_args = self.parser.parseString(self.operation + " " + str(self.job))
        job_args = job_args[0:]

        if len(job_args) != 2:
            self.logger.error('ERROR! must have only 2 args (operation, params). skipping! %s', job_args)
            self.job = None
            self.process_job()
            return

        operation_params = parse_param(job_args[1])
        if len(operation_params) != self.operation_args_array_length:
            self.logger.error('ERROR! must have exactly %d operation args, not %d. skipping! %s', self.operation_args_array_length, len(operation_params), job_args)
            self.job = None
            self.process_job()
            return

        args = [self.smart_contract_hash] + job_args
        self.logger.debug('processing job: %s', args)
        result = self.test_invoke(args, self.expected_result_count, self.test_only, self.from_addr)

        if not result:
            # transaction failed? wallet probably out-of-sync (insufficient funds) so reload it
            self.wallet_needs_recovery = True
        else:
            # this job has been invoked, so clear it out. on to the next.
            self.job = None
            if self.test_only:
                # when testing but not relaying transactions, we just continue to the next job
                self.jobs_processed += 1
                self.process_job()
            else:
                # transaction successfully relayed? then let's set the tx Hash that we're waiting for
                self.tx_processing = result.Hash


def main():
    bulk_process = BulkProcess()
    bulk_process.run()


if __name__ == "__main__":
    main()
