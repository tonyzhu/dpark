import time 
import sys
import logging
import socket
from operator import itemgetter

logger = logging.getLogger("job")

TASK_STARTING = 0
TASK_RUNNING  = 1
TASK_FINISHED = 2
TASK_FAILED   = 3
TASK_KILLED   = 4
TASK_LOST     = 5

class Job:
    def __init__(self):
        self.id = self.newJobId()
        self.start = time.time()

    def slaveOffer(self, s, availableCpus):
        raise NotImplementedError

    def statusUpdate(self, t):
        raise NotImplementedError

    def error(self, code, message):
        raise NotImplementedError
    
    nextJobId = 0
    @classmethod
    def newJobId(cls):
        cls.nextJobId += 1
        return cls.nextJobId

LOCALITY_WAIT = 0
WAIT_FOR_RUNNING = 15
MAX_TASK_FAILURES = 4
CPUS_PER_TASK = 1


# A Job that runs a set of tasks with no interdependencies.
class SimpleJob(Job):

    def __init__(self, sched, tasks):
        Job.__init__(self)
        self.sched = sched
        self.tasks = tasks

        for t in tasks:
            t.tried = 0
            t.used = 0

        self.launched = [False] * len(tasks)
        self.finished = [False] * len(tasks)
        self.numFailures = [0] * len(tasks)
        self.blacklist = [[] for i in xrange(len(tasks))]
        self.tidToIndex = {}
        self.numTasks = len(tasks)
        self.tasksLaunched = 0
        self.tasksFinished = 0
        self.total_used = 0

        self.lastPreferredLaunchTime = time.time()

        self.pendingTasksForHost = {}
        self.pendingTasksWithNoPrefs = []
        self.allPendingTasks = []

        self.failed = False
        self.causeOfFailure = ""
        self.last_check = 0

        for i in range(len(tasks)):
            self.addPendingTask(i)
        self.host_cache = {}

    @property
    def taskEverageTime(self):
        if not self.tasksFinished:
            return 10
        return max(self.total_used / self.tasksFinished, 5)

    def addPendingTask(self, i):
        loc = self.tasks[i].preferredLocations()
        if not loc:
            self.pendingTasksWithNoPrefs.append(i)
        else:
            for host in loc:
                self.pendingTasksForHost.setdefault(host, []).append(i)
        self.allPendingTasks.append(i)

    def getPendingTasksForHost(self, host):
        try:
            return self.host_cache[host]
        except KeyError:
            v = self._getPendingTasksForHost(host)
            self.host_cache[host] = v
            return v

    def _getPendingTasksForHost(self, host):
        try:
            h, hs, ips = socket.gethostbyname_ex(host)
        except Exception:
            h, hs, ips = host, [], []
        tasks = sum((self.pendingTasksForHost.get(h, []) 
            for h in [h] + hs + ips), [])
        st = {}
        for t in tasks:
            st[t] = st.get(t, 0) + 1
        ts = sorted(st.items(), key=itemgetter(1), reverse=True)
        return [t for t,_ in ts ]

    def findTaskFromList(self, l, host):
        for i in l:
            if not self.launched[i] and not self.finished[i] and host not in self.blacklist[i]:
                self.blacklist[i].append(host)
                return i

    def findTask(self, host, localOnly):
        localTask = self.findTaskFromList(self.getPendingTasksForHost(host), host)
        if localTask is not None:
            return localTask, True
        noPrefTask = self.findTaskFromList(self.pendingTasksWithNoPrefs, host)
        if noPrefTask is not None:
            return noPrefTask, True
        if not localOnly:
            return self.findTaskFromList(self.allPendingTasks, host), False
#        else:
#            print repr(host), self.pendingTasksForHost
        return None, False

    # Respond to an offer of a single slave from the scheduler by finding a task
    def slaveOffer(self, host, availableCpus): 
        now = time.time()
        localOnly = (now - self.lastPreferredLaunchTime < LOCALITY_WAIT)
        i, preferred = self.findTask(host, localOnly)
        if i is not None:
            task = self.tasks[i]
            task.status = TASK_STARTING
            task.start = now
            task.host = host
            task.tried += 1
            prefStr = preferred and "preferred" or "non-preferred"
            logger.debug("Starting task %d:%d as TID %s on slave %s (%s)", 
                self.id, i, task, host, prefStr)
            self.tidToIndex[task.id] = i
            self.launched[i] = True
            self.tasksLaunched += 1
            if preferred:
                self.lastPreferredLaunchTime = now
            return task
        logger.debug("no task found %s", localOnly)

    def statusUpdate(self, tid, tried, status, reason=None, result=None, update=None):
        logger.debug("job status update %s %s %s", tid, status, reason)
        if tid not in self.tidToIndex:
            logger.error("invalid tid: %s", tid)
            return
        i = self.tidToIndex[tid]
        if self.finished[i]:
            if status == TASK_FINISHED:
                logger.info("Task %d is already finished, ignore it", tid)
            return 

        task = self.tasks[i]
        task.status = status
        # when checking, task been masked as not launched
        if not self.launched[i]:
            self.launched[i] = True
            self.tasksLaunched += 1 
        
        if status == TASK_FINISHED:
            self.taskFinished(tid, tried, result, update)
        elif status in (TASK_LOST, 
                    TASK_FAILED, TASK_KILLED):
            self.taskLost(tid, tried, status, reason)
        
        task.start = time.time()

    def taskFinished(self, tid, tried, result, update):
        i = self.tidToIndex[tid]
        self.finished[i] = True
        self.tasksFinished += 1
        task = self.tasks[i]
        task.used += time.time() - task.start
        self.total_used += task.used
        logger.info("Task %s finished in %.1fs (%d/%d)",
            tid, task.used, self.tasksFinished, self.numTasks)
        from schedule import Success
        self.sched.taskEnded(task, Success(), result, update)

        for t in range(task.tried):
            if t + 1 != tried:
                self.sched.killTask(self.id, task.id, t + 1)

        if self.tasksFinished == self.numTasks:
            ts = [t.used for t in self.tasks]
            tried = [t.tried for t in self.tasks]
            logger.info("Job %d finished in %.1fs: min=%.1fs, avg=%.1fs, max=%.1fs, maxtry=%d",
                self.id, time.time()-self.start, 
                min(ts), sum(ts)/len(ts), max(ts), max(tried))
            self.sched.jobFinished(self)

    def taskLost(self, tid, tried, status, reason):
        index = self.tidToIndex[tid]
        logger.warning("Lost Task %d (task %d:%d:%s) %s", index, self.id, tid, tried, reason)
        self.launched[index] = False
        if self.tasksLaunched == self.numTasks:    
            self.sched.requestMoreResources()
        self.tasksLaunched -= 1

        from schedule import FetchFailed
        if isinstance(reason, FetchFailed):
            logger.warning("Loss was due to fetch failure from %s",
                reason.serverUri)
            self.sched.taskEnded(self.tasks[index], reason, None, None)
            #FIXME, handler fetch failure
            self.finished[index] = True
            self.tasksFinished += 1
            if self.tasksFinished == self.numTasks:
                self.sched.jobFinished(self)
            return
        
        if status == TASK_FAILED:
            logger.warning("task %s failed with: %s", 
                self.tasks[index], reason)
        if status in (TASK_FAILED, TASK_LOST):
            self.numFailures[index] += 1
            if self.numFailures[index] > MAX_TASK_FAILURES:
                logger.error("Task %d failed more than %d times; aborting job", 
                    index, MAX_TASK_FAILURES)
                self.abort("Task %d failed more than %d times" 
                    % (index, MAX_TASK_FAILURES))

    def check_task_timeout(self):
        now = time.time()
        if self.last_check + 5 > now:
            return False
        self.last_check = now
        
        n = self.launched.count(True)
        if n != self.tasksLaunched:
            logger.error("bug: tasksLaunched(%d) != %d", self.tasksLaunched, n)
            self.tasksLaunched = n

        for i in xrange(self.numTasks):
            task = self.tasks[i]
            if (self.launched[i] and task.status == TASK_STARTING
                    and task.start + WAIT_FOR_RUNNING < now):
                logger.warning("task %d timeout %.1f (at %s), re-assign it", 
                        task.id, now - task.start, task.host)
                self.launched[i] = False
                self.tasksLaunched -= 1

        if self.tasksFinished > self.numTasks / 3:
            avg = self.taskEverageTime
            tasks = sorted((task.start, i, task) 
                for i,task in enumerate(self.tasks) 
                if self.launched[i] and not self.finished[i])
            for _t, idx, task in tasks:
                used = now - task.start
                #logger.debug("task %s used %.1f (avg = %.1f)", task.id, used, avg)
                if used > avg * (task.tried + 1) and used > 30:
                    # re-submit timeout task
                    if task.tried <= MAX_TASK_FAILURES:
                        logger.warning("re-submit task %s for timeout %.1f, try %d",
                            task.id, used, task.tried)
                        task.used += used
                        task.start = now
                        self.launched[idx] = False
                        self.tasksLaunched -= 1 
                    else:
                        logger.error("tast %s timeout, aborting job %s",
                            task, self.id)
                        self.abort("task %s timeout" % task)
                else:
                    break
        return self.tasksLaunched < n

    def abort(self, message):
        logger.error("abort the job: %s", message)
        self.failed = True
        self.causeOfFailure = message
        self.sched.jobFinished(self)
        self.sched.shutdown()
