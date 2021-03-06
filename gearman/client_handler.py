import collections
import time
import logging

from gearman.command_handler import GearmanCommandHandler
from gearman.constants import JOB_PENDING, JOB_QUEUED, JOB_FAILED, JOB_COMPLETE
from gearman.errors import InvalidClientState
from gearman.protocol import GEARMAN_COMMAND_GET_STATUS, submit_cmd_for_background_priority

gearman_logger = logging.getLogger(__name__)

class GearmanClientCommandHandler(GearmanCommandHandler):
    """Maintains the state of this connection on behalf of a GearmanClient"""
    def __init__(self, connection_manager=None):
        super(GearmanClientCommandHandler, self).__init__(connection_manager=connection_manager)

        # When we first submit jobs, we don't have a handle assigned yet... these handles will be returned in the order of submission
        self.requests_awaiting_handles = collections.deque()
        self.handle_to_request_map = dict()

    ##################################################################
    ##### Public interface methods to be called by GearmanClient #####
    ##################################################################
    def send_job_request(self, current_request):
        """Register a newly created job request"""
        gearman_job = current_request.job

        # Handle the I/O for requesting a job - determine which COMMAND we need to send
        cmd_type = submit_cmd_for_background_priority(current_request.background, current_request.priority)

        outbound_data = self.encode_data(gearman_job.data)
        self.send_command(cmd_type, task=gearman_job.task, unique=gearman_job.unique, data=outbound_data)

        # Once this command is sent, our request needs to wait for a handle
        self.requests_awaiting_handles.append(current_request)

    def send_get_status_of_job(self, current_request):
        """Forward the status of a job"""
        self.send_command(GEARMAN_COMMAND_GET_STATUS, job_handle=current_request.job.handle)

    def get_requests(self):
        """Fetch all requests that this CommandHandler is aware of"""
        pending_requests = self.requests_awaiting_handles
        inflight_requests = self.handle_to_request_map.itervalues()
        return pending_requests, inflight_requests

    ##################################################################
    ## Gearman command callbacks with kwargs defined by protocol.py ##
    ##################################################################
    def _assert_request_state(self, current_request, expected_state):
        if current_request.state != expected_state:
            raise InvalidClientState('Expected handle (%s) to be in state %r, got %s' % (current_request.job.handle, expected_state, current_request.state))

    def recv_job_created(self, job_handle):
        if not self.requests_awaiting_handles:
            raise InvalidClientState('Received a job_handle with no pending requests')

        # If our client got a JOB_CREATED, our request now has a server handle
        current_request = self.requests_awaiting_handles.popleft()
        self._assert_request_state(current_request, JOB_PENDING)

        # Update the state of this request
        current_request.job.handle = job_handle
        current_request.state = JOB_QUEUED
        self.handle_to_request_map[job_handle] = current_request

        return True

    def recv_work_data(self, job_handle, data):
        # Queue a WORK_DATA update
        current_request = self.handle_to_request_map[job_handle]
        self._assert_request_state(current_request, JOB_QUEUED)

        current_request.data_updates.append(self.decode_data(data))

        return True

    def recv_work_warning(self, job_handle, data):
        # Queue a WORK_WARNING update
        current_request = self.handle_to_request_map[job_handle]
        self._assert_request_state(current_request, JOB_QUEUED)

        current_request.warning_updates.append(self.decode_data(data))

        return True

    def recv_work_status(self, job_handle, numerator, denominator):
        # Queue a WORK_STATUS update
        current_request = self.handle_to_request_map[job_handle]
        self._assert_request_state(current_request, JOB_QUEUED)

        # The protocol spec is ambiguous as to what type the numerator and denominator is...
        # For now, let's cast to a float as I its safe to assume that we need to get a number back here
        status_tuple = (float(numerator), float(denominator))
        current_request.status_updates.append(status_tuple)

        return True

    def recv_work_complete(self, job_handle, data):
        # Update the state of our request and store our returned result
        current_request = self.handle_to_request_map[job_handle]
        self._assert_request_state(current_request, JOB_QUEUED)

        current_request.result = self.decode_data(data)
        current_request.state = JOB_COMPLETE

        return True

    def recv_work_fail(self, job_handle):
        # Update the state of our request and mark this job as failed
        current_request = self.handle_to_request_map[job_handle]
        self._assert_request_state(current_request, JOB_QUEUED)

        current_request.state = JOB_FAILED

        return True

    def recv_work_exception(self, job_handle, data):
        # Using GEARMAND_COMMAND_WORK_EXCEPTION is not recommended at time of this writing [2010-02-24]
        # http://groups.google.com/group/gearman/browse_thread/thread/5c91acc31bd10688/529e586405ed37fe
        #
        current_request = self.handle_to_request_map[job_handle]
        self._assert_request_state(current_request, JOB_QUEUED)

        current_request.exception = self.decode_data(data)

        return True

    def recv_status_res(self, job_handle, known, running, numerator, denominator):
        # If we received a STATUS_RES update about this request, update our known status
        current_request = self.handle_to_request_map[job_handle]
        self._assert_request_state(current_request, JOB_QUEUED)

        # Make our server_status response Python friendly
        current_request.server_status = {
            'handle': job_handle,
            'known': bool(known == '1'),
            'running': bool(running == '1'),
            'numerator': float(numerator),
            'denominator': float(denominator),
            'time_received': time.time()
        }
        return True
