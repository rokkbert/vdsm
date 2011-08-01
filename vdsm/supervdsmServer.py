#!/usr/bin/python
import logging
import logging.config
import sys
import os
import errno
import threading
from time import sleep
import signal
from multiprocessing import Pipe, Process

from storage.multipath import getScsiSerial as _getScsiSerial
from storage.iscsi import forceIScsiScan as _forceIScsiScan
from supervdsm import _SuperVdsmManager, PIDFILE, ADDRESS
from storage.fileUtils import chown, open_ex, resolveGid, resolveUid
from storage.fileUtils import validateAccess as _validateAccess
from constants import METADATA_GROUP, METADATA_USER
from storage.devicemapper import _removeMapping, _getPathsStatus
import configNetwork
from config import config

RUN_AS_TIMEOUT= config.getint("irs", "process_pool_timeout")
class Timeout(RuntimeError): pass

def logDecorator(func):
    callbackLogger = logging.getLogger("SuperVdsm.ServerCallback")
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except:
            callbackLogger.error("Error in %s", func.__name__, exc_info=True)
            raise
    return wrapper

KB = 2**10
TEST_BUFF_LEN = 4 * KB
LOG_CONF_PATH = "/etc/vdsm/logger.conf"
class _SuperVdsm(object):
    @logDecorator
    def getScsiSerial(self, *args, **kwargs):
        return _getScsiSerial(*args, **kwargs)

    @logDecorator
    def forceIScsiScan(self, *args, **kwargs):
        return _forceIScsiScan(*args, **kwargs)

    @logDecorator
    def testReadDevices(self, devices):
        for device in devices:
            with open_ex(device, "dr") as f:
                f.seek(TEST_BUFF_LEN)
                if len(f.read(TEST_BUFF_LEN)) < TEST_BUFF_LEN:
                    raise OSError("Could not read from device %s" % device)

    @logDecorator
    def removeDeviceMapping(self, devName):
        return _removeMapping(devName)

    @logDecorator
    def getPathsStatus(self):
        return _getPathsStatus()

    @logDecorator
    def addNetwork(self, bridge, options):
        return configNetwork.addNetwork(bridge, **options)

    @logDecorator
    def delNetwork(self, bridge, options):
        return configNetwork.delNetwork(bridge, **options)

    @logDecorator
    def editNetwork(self, oldBridge, newBridge, options):
        return configNetwork.editNetwork(oldBridge, newBridge, **options)

    @logDecorator
    def setupNetworks(self, networks={}, bondings={}, options={}):
        return configNetwork.setupNetworks(networks, bondings, **options)

    def _runAs(self, user, groups, func, args=(), kwargs={}):
        def child(pipe):
            res = ex = None
            try:
                uid = resolveUid(user)
                if groups:
                     gids = map(resolveGid, groups)

                     os.setgid(gids[0])
                     os.setgroups(gids)
                os.setuid(uid)

                res = func(*args, **kwargs)
            except BaseException, e:
                ex = e

            pipe.send((res, ex))
            pipe.recv()

        pipe, hisPipe = Pipe()
        proc = Process(target=child, args=(hisPipe,))
        proc.start()

        if not pipe.poll(RUN_AS_TIMEOUT):
            try:
                os.kill(proc.pid, errno.SIGKILL)
            except OSError, e:
                # If it didn't fail because process is already dead
                if e.errno != errno.ESRCH:
                    raise

            raise Timeout()

        res, err = pipe.recv()
        pipe.send("Bye")
        proc.terminate()
        if err is not None:
            raise err

        return res

    @logDecorator
    def validateAccess(self, user, groups, *args, **kwargs):
        return self._runAs(user, groups, _validateAccess, args=args, kwargs=kwargs)

    @logDecorator
    def setSafeNetworkConfig(self):
        return configNetwork.setSafeNetworkConfig()

def __pokeParent(parentPid):
    try:
        while True:
            os.kill(parentPid, 0)
            sleep(2)
    except Exception:
        os.unlink(ADDRESS)
        os.kill(os.getpid(), signal.SIGTERM)

def main():
    try:
        logging.config.fileConfig(LOG_CONF_PATH)
    except:
        logging.basicConfig(filename='/dev/stdout', filemode='w+', level=logging.DEBUG)
        log = logging.getLogger("SuperVdsm.Server")
        log.warn("Could not init proper logging", exc_info=True)

    log = logging.getLogger("SuperVdsm.Server")
    try:
        log.debug("Making sure I'm root")
        if os.geteuid() != 0:
            sys.exit(errno.EPERM)

        log.debug("Parsing cmd args")
        authkey, parentPid = sys.argv[1:]

        log.debug("Creating PID file")
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()) + "\n")

        log.debug("Cleaning old socket")
        if os.path.exists(ADDRESS):
            os.unlink(ADDRESS)

        log.debug("Setting up keep alive thread")
        monThread = threading.Thread(target=__pokeParent, args=[int(parentPid)])
        monThread.setDaemon(True)
        monThread.start()

        log.debug("Creating remote object manager")
        manager = _SuperVdsmManager(address=ADDRESS, authkey=authkey)
        manager.register('instance', callable=_SuperVdsm)

        server = manager.get_server()
        servThread = threading.Thread(target=server.serve_forever)
        servThread.setDaemon(True)
        servThread.start()

        chown(ADDRESS, METADATA_USER, METADATA_GROUP)

        log.debug("Started serving super vdsm object")
        servThread.join()
    except Exception:
        log.error("Could not start Super Vdsm", exc_info=True)
        sys.exit(1)
    finally:
        try:
            os.unlink(ADDRESS)
        except OSError:
            pass

if __name__ == '__main__':
    main()
