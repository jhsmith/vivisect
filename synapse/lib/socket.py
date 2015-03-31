'''
Synapse extensible/hookable sockets.
'''
import time
import socket
import msgpack
import selectors
import traceback

import synapse.lib.common as s_common
import synapse.event.dist as s_evtdist
import synapse.lib.threads as s_threads

class SocketError(Exception):pass
class SocketClosed(SocketError):pass

class Socket(s_evtdist.EventDist):
    '''
    An extensible socket object.

    Event Names:
    sock:conn       - the socket is newly connected
    sock:accept     - the socket accepted a newsock
    sock:tx         - the socket returned from sending data
    sock:rx         - the socket returned from recieving data
    sock:shut       - the socket connection has terminated
    '''
    def __init__(self, sock=None):
        if sock == None:
            sock = socket.socket()

        self.sock = sock
        self._sock_info = {}

        s_evtdist.EventDist.__init__(self)

    def info(self, prop, valu=None):
        '''
        Get/Set arbitrary/app-layer info for this socket.

        Example:

            s = Socket()
            s.info('woot',5) # set by specifying valu

            # ...some time later...
            x = s.info('woot')

        '''
        if valu != None:
            self._sock_info[prop] = valu

        return self._sock_info.get(prop)

    def connect(self, sockaddr):
        self.sock.connect(sockaddr)
        self.fire('sock:conn', sock=self)

    def accept(self):
        s,addr = self.sock.accept()

        sock = Socket(s)
        self.fire('sock:accept', sock=self, newsock=sock)

        sock.fire('sock:conn', sock=sock)
        return sock

    def fileno(self):
        return self.sock.fileno()

    def recv(self, size=None):
        buf = self._sock_recv(size)
        self.fire('sock:rx', size=size, buf=buf, sock=self)
        return buf

    def send(self, buf):
        sent = self._sock_send(buf)
        self.fire('sock:tx', sent=sent, buf=buf, sock=self)
        return sent

    def emit(self, obj):
        '''
        Use msgpack to serialize an object to the socket.

        Example:
            x = (1,2,3,'qwer')
            sock.emit(x)
        '''
        try:
            self.sendall( msgpack.packb(obj,use_bin_type=True) )
            return True

        except SocketError as e:
            return False

    def _sock_send(self, buf):
        try:
            return self.sock.send(buf)
        except socket.error as e:
            raise SocketClosed()

    def shutdown(self, how):
        self.sock.shutdown(how)
        self.fire('sock:shut', sock=self)

    def close(self):
        self.sock.close()

    def teardown(self):

        if not self.info('listen'):
            try:
                self.sock.shutdown(socket.SHUT_WR)
            except Exception as e:
                print('teardown shutdown: %s' % e)

        try:
            self.sock.close()
        except Exception as e:
            print('teardown close: %s' % e)

        self.fire('sock:shut', sock=self)

    def _sock_recv(self, size):
        try:
            return self.sock.recv(size)
        except socket.error as e:
            raise SocketClosed()

    def sendall(self, buf):
        sent = 0
        size = len(buf)
        while sent < size:
            off = self.send(buf)
            sent += off

    def recvall(self, size):
        buf = b''
        while len(buf) < size:
            x = self.recv(size-len(buf))
            if not x:
                raise SocketClosed()
            buf += x
        return buf

    # socket API pass throughs...
    def settimeout(self, t):
        self.sock.settimeout(t)

class Server(s_evtdist.EventQueue):
    '''
    A socket server using multiplexed IO and EventDist.
    '''

    def __init__(self, sockaddr, pool=10, timeout=None):
        self.sock = socket.socket()
        self.srvthr = None
        self.seltor = None
        self.wakesock = None
        self.srvshut = False
        self.timeout = timeout
        self.sockaddr = sockaddr
        s_evtdist.EventQueue.__init__(self,pool=pool)

    def fini(self):
        self.srvshut = True
        self.sock.close()
        self.wakesock.close()
        self.seltor.close()
        self.srvthr.join()
        s_evtdist.EventQueue.fini(self)

    def synRunServer(self):
        self.sock.bind( self.sockaddr )
        self.sockaddr = self.sock.getsockname()
        self.sock.listen(100)

        self.srvthr = s_threads.fireWorkThread(self._runServerLoop)
        return self.sockaddr

    def synWaitServer(self):
        '''
        Wait for the server to terminate ( but do not instruct it to ).
        '''
        self.srvthr.join()

    def synGetServAddr(self):
        return self.sockaddr

    def _runServerLoop(self):

        self.seltor = selectors.DefaultSelector()
        key = self.seltor.register(self.sock, selectors.EVENT_READ)

        self.wakesock,s2 = socketpair()
        self.seltor.register(s2, selectors.EVENT_READ)

        #s1,s2 = socket.socketpair()
        # stuff a socket into the selector to wake on close

        while True:

            for key,events in self.seltor.select():

                if self.srvshut:
                    break

                if key.data == None:
                    conn,addr = key.fileobj.accept()
                    # TIMEOUT
                    sock = Socket(conn)
                    # re dist all socket events to self
                    sock.link(self)

                    sock.fire('sock:conn', addr=addr, sock=sock)

                    unpacker = msgpack.Unpacker(use_list=False,encoding='utf8')
                    sockdata = {'sock':sock,'unpacker':unpacker,'serv':self,'addr':addr}

                    self.seltor.register(conn, selectors.EVENT_READ, data=sockdata)
                    continue

                sock = key.data['sock']
                buf = sock.recv(102400)
                if not buf:
                    self.seltor.unregister(key.fileobj)
                    self.fire('sock:shut',**key.data)
                    key.fileobj.close()
                    continue
                    
                unpk = key.data['unpacker']

                unpk.feed(buf)
                for msg in unpk:
                    sock.fire('sock:msg', msg=msg, sock=sock)

            if self.srvshut:
                s2.close()
                self.fire('serv:shut', serv=self)
                return

    def synGetServAddr(self):
        '''
        Retrieve a tuple of (host,port) for this server.

        NOTE: the "host" part is the return value from
              socket.gethostname()
        '''
        host = socket.gethostname()
        return (host,self.sockaddr[1])

class Plex(s_evtdist.EventDist):
    '''
    Manage multiple Sockets using a multi-plexor IO thread.
    '''
    def __init__(self):
        s_evtdist.EventDist.__init__(self)

        self._plex_sel = selectors.DefaultSelector()
        self._plex_shut = False
        self._plex_socks = set()

        self._plex_wake, self._plex_s2 = socketpair()

        self._plex_thr = s_threads.fireWorkThread( self._plexMainLoop )

        self.on('sock:conn', self._on_sockconn)
        self.on('sock:shut', self._on_sockshut)
        self.on('sock:accept', self._on_sockaccept)

    def connect(self, host, port):
        '''
        Create and connect a new TCP Socket within the Plex.

        Example:

            plex = Plex()
            plex.connect(host,port)

        '''
        sock = Socket()
        sock.link(self)
        sock.connect( (host,port) )

    def listen(self, host='0.0.0.0', port=0):
        '''
        Create and bind a new TCP Server within the Plex.

        Example:

            plex = Plex()
            sockaddr = plex.listen(port=3333)

        Notes:

            * if port=0, an OS chosen ephemeral port will be used

        '''

        s = socket.socket()
        s.bind( (host,port) )
        s.listen( 100 )

        port = s.getsockname()[1]

        sock = Socket(s)
        sock.info('listen', True)

        sock.link( self )

        self._sock_on( sock )
        return s.getsockname()

    def wrap(self, s):
        sock = Socket(s)
        sock.link(self)
        sock.fire('sock:conn', sock=sock)
        return sock

    #def sock(self, sock):

    #def addPlexSock(self, sock, listen=False):
        #'''
        #Add a socket to the multiplexor.
#
        #Example:
#
            #plex.addPlexSock( sock )
#
        #Use listen=True when adding a listening socket which should
        #call accept() on read events.
#
        #'''
        #sock = Socket(sock)
        #sock.info('listen',listen)
        #self._init_plexsock( sock )
#
    #def runPlexMain(self, thread=False):
        #if thread:
             #s_threads.fireWorkThread( self.runPlexMain, thread=False )

    def _on_sockaccept(self, event):
        sock = event[1].get('newsock')
        sock.link( self )

    def _on_sockconn(self, event):
        sock = event[1].get('sock')
        self._sock_on(sock)

    def _on_sockshut(self, event):
        sock = event[1].get('sock')
        self._sock_off(sock)

    def _sock_off(self, sock):
        self._plex_sel.unregister(sock)
        self._plex_socks.remove(sock)
        self._plexWake()

    def _sock_on(self, sock):

        unpacker = msgpack.Unpacker(use_list=False, encoding='utf8')
        sock.info('unpacker', unpacker)

        self._plex_sel.register(sock, selectors.EVENT_READ)
        self._plex_socks.add(sock)
        self._plexWake()

    def _plexWake(self):
        self._plex_wake.sendall(b'\x00')

    def _plexMainLoop(self):

        self._plex_sel.register( self._plex_s2, selectors.EVENT_READ )

        while True:

            for key,events in self._plex_sel.select(timeout=1):

                if self._plex_shut:
                    break

                sock = key.fileobj
                if sock == self._plex_s2:
                    sock.recv(1024)
                    continue

                if sock.info('listen'):
                    # his sock:conn event handles reg
                    sock.accept()
                    continue

                buf = sock.recv(102400)

                if not buf:
                    # his sock:shut handles unreg
                    sock.teardown()
                    continue

                unpk = sock.info('unpacker')

                unpk.feed( buf )

                for msg in unpk:
                    sock.fire('sock:msg', plex=self, msg=msg, sock=sock)

            if self._plex_shut:
                break

        self._plex_s2.close()
        for sock in list(self._plex_socks):
            sock.teardown()

        self._plex_sel.close()

    def fini(self):
        self._plex_shut = True
        self._plex_wake.close()
        self._plex_thr.join()

def _sockpair():
    s = socket.socket()
    s.bind(('127.0.0.1',0))
    s.listen(1)

    s1 = socket.socket()
    s1.connect( s.getsockname() )

    s2 = s.accept()[0]

    s.close()
    return s1,s2

def socketpair():
    '''
    Standard sockepair() on posix systems, and pure shinanegans on windows.
    '''
    try:
        return socket.socketpair()
    except AttributeError as e:
        return _sockpair()

def connect(host,port):
    '''
    Instantiate a Socket and connect to the given host:port.
    '''
    sock = Socket()
    sock.connect( (host,port) )
    return sock
