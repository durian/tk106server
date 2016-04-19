#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
import logging
import logging.handlers
import sys
import socketserver
from socket import timeout
import time
import datetime
import calendar
import select
import getopt
import os
import signal
import operator
import shutil
import re
from collections import namedtuple
try:
    import pickle as pickle
except:
    import pickle
from POSHandler import *

import socket
import threading
import queue
import math
import json

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)-8s %(message)s')
#
rtfile  = logging.handlers.RotatingFileHandler("./tk106.log",maxBytes=1024*1024*25, backupCount=9)
console = logging.StreamHandler()
#
glogger = logging.getLogger('TK106')
rtformat='%(asctime)s %(levelname)-8s %(message)s'
rtformatter = logging.Formatter(fmt=rtformat)
rtfile.setFormatter(rtformatter)
glogger.addHandler(rtfile)
glogger.propagate = False #stop console log for this one
# Set up console
cformat='%(asctime)s %(message)s'
cformatter = logging.Formatter(fmt=cformat, datefmt="%Y%m%d %H:%M:%S")
console.setFormatter(cformatter)
console.setLevel(logging.INFO)
glogger.addHandler(console)
#

class Info(object):
    def __init__(self):
        self.imeis        = []   #imei nrs if right protocol
        self.imeis_info   = {} # imei->(info0,info1)
        self.serials      = [] #the directory names
        self.serials_info = {} # serial->(info0,info1)
    def add_imei(self, imei):
        self.imeis.append(imei)
    def del_imei(self, imei):
        try:
            self.imeis.remove(imei)
        except ValueError:
            pass
    def add_serial(self, serial):
        self.serials.append(serial)
    def del_serial(self, serial):
        try:
            self.serials.remove(serial)
        except ValueError:
            pass
    def __str__(self):
        return repr(self.serials+self.imeis)
    def __repr__(self):
        return self.__str__()
    
class RingBuffer(object):
    '''
    x = RingBuffer(5)
    x.append(1)
    print( x.get() )
    '''
    def __init__(self,size_max=10000):
        self.max = size_max
        self.data = []
    def _full_append(self, x):
        self.data[self.cur] = x
        self.cur = (self.cur+1) % self.max
    def _full_get(self):
        return self.data[self.cur:]+self.data[:self.cur]
    def append(self, x):
        self.data.append(x)
        if len(self.data) == self.max:
            self.cur = 0
            # Permanently change self's methods from non-full to full
            self.append = self._full_append
            self.get    = self._full_get
    def get(self):
        return self.data
    
#Pos = namedtuple('Pos', 'imei, lat, lon, spd, bearing, acc, idx')
class Pos:
    def __init__(self, imei, lat, lon, spd, bea, acc, alt, idx):
        self.imei = imei
        self.lat = lat
        self.lon = lon
        self.spd = spd
        self.bearing = bea
        self.acc = acc
        self.alt = alt
        self.idx = idx
    def __str__(self):
        # removed %s for self.imei
        return '%.6f %.6f %3i km/h %3i deg %5i m (%05i)' % (self.lat, self.lon, self.spd, self.bearing, self.alt, self.idx)
    def __repr__(self):
        return self.__str__()

class MyWatcher(socketserver.BaseRequestHandler):
    def handle(self):
        global lock
        self.data = self.request.recv(4096)
        glogger.info('WATCHER: ' + str(self.data))
        #self.request.sendall(str(self.data)[::-1].encode('utf-8'))
        # check and put on queue, like msgs? or directly? via do_cmd?
        # (stamp, (imei, cmd) )
        sdata = b2s(self.data)
        sdata = sdata.strip()
        bits = sdata.split('\t')
        if len(bits) == 3:
            item = ( int(bits[0])+time.time(), ( int(bits[1]), bits[2] ) )
            glogger.info('WATCHER: ' + repr(item))
            if lock.acquire(True,1.0):
                self.server._pq.put(item) 
                lock.release()
                self.request.sendall("OK\r\n".encode('utf-8'))
                return
            else:
                glogger.info( "No lock aqcuired." )
        if len(bits) == 1:
            if bits[0] == "INFO":
                global info
                print( info )
                self.request.sendall((json.dumps(info.imeis, separators=(',', ':'))+"\r\n").encode('utf-8'))
                self.request.sendall((json.dumps(info.serials, separators=(',', ':'))+"\r\n").encode('utf-8'))
                self.request.sendall("OK\r\n".encode('utf-8'))
                return
            elif bits[0] == "QUIT":
                global sunshine
                sunshine = False
                self.request.sendall("OK\r\n".encode('utf-8'))
                return
        self.request.sendall("ERROR\r\n".encode('utf-8'))
        
class NOPROT:
    def __init__(self, logger, imei):
        self.logger = logger
        self.msg_cnt = 0
        self.imei = str(imei)
        self.pos = None
        self.mileage      = 0
        self.history = RingBuffer() # 10*170 bytes, 1000*170 = 166 kB (bz2)
        self.info("New NOPROT()")
    def info(self, msg):
        self.logger.info("NPROT("+self.imei[0:6]+"): "+msg)        
    def parse(self, msg):
        if msg:
            self.history.append(msg)
            self.msg_cnt += 1

    
class TK106:
    def __init__(self, logger, imei):
        self.logger = logger
        self.msg_cnt = 0
        self.expected_event_code = 0 #35? 'AAA'? 'A21,OK' ?
        #17 Low Battery
        #18 Low External Battery
        self.pos          = None
        self.prev_pos     = None
        self.imei         = str(imei)
        self.data_id      = None
        self.data_len     = 0
        self.cmd_type     = None
        self.evt_code     = 0
        self.lat          = None
        self.lon          = None
        self.datetime     = None
        self.datetime_str = None
        self.pos_status   = None
        self.num_sat      = None
        self.gsm_signal   = None
        self.speed        = None
        self.direction    = None
        self.hor_acc      = None
        self.alt          = None
        self.mileage      = None
        self.run_time     = None
        self.base_stat    = None
        self.port_stat    = None
        self.analog_in    = None
        self.checksum     = None
        self.history      = RingBuffer()
        self.info("New TK106()")
    def info(self, msg):
        self.logger.info("TK106("+self.imei[0:8]+"): "+msg)
    def parse(self, msg):
        if not msg:
            return
        #$$R154,0ximei,AAA,35,55.069868,12.402848,151112085528,A,8,18,0,193,1.6,78,24,38067,240|24|2FA9|0B9C,0000,0002|0002|0000|097E|0000,00000001,,1,0000*E3
        # $$A30,0ximei,A70,,,,,*F7
        bits = msg.split(",")
        if len(bits) < 5:
            return
        self.data_id = bits[0][2]         # $$A123
        self.data_len = int(bits[0][3:])  # $$A123
        #self.imei = bits[1]              # we already have it
        self.cmd_type = bits[2]           # AAA
        try:
            self.evt_code = int(bits[3])  # 35
        except ValueError:
            self.evt_code = -1

        #self.info(repr([self.data_id, self.data_len, self.cmd_type, self.evt_code]))
        self.history.append(msg)
        
        if self.evt_code in [17, 18]:
            #low (externa) batt
            self.info( "CODE: LOW BATTERY" )

        if self.evt_code == 19:
            self.info( "CODE: SPEEDING" )
            
        if self.evt_code == 20:
            self.info( "CODE: ENTER GEO FENCE" )
        if self.evt_code == 21:
            self.info( "CODE: EXIT GEO FENCE" )

        if self.evt_code == 22:
            self.info( "CODE: EXT BATT ON" )
        if self.evt_code == 23:
            self.info( "CODE: EXT BATT CUT" )

        if self.evt_code == 24:
            self.info( "CODE: LOSE GPS SIGNAL" )
            
        if self.evt_code == 28:
            self.info( "CODE: GPS ANTENNA CUT" )

        if self.evt_code == 29:
            self.info( "CODE: DEVICE REBOOT" )

        if self.evt_code == 41:
            self.info( "CODE: STOP MOVING" )
        if self.evt_code == 42:
            self.info( "CODE: START MOVING" )

        if self.evt_code == 44:
            self.info( "CODE: GPS JAMMED" )
            
        if len(bits) < 23:
            return
        self.lat          = float(bits[4])  # 53.069868
        self.lon          = float(bits[5])  # 12.402848
        self.datetime     = bits[6]         # 151112085528
        self.datetime_str = "20"+self.datetime[0:6]+"@"+self.datetime[6:8]+":"+self.datetime[8:10]+":"+self.datetime[10:12]
        self.pos_status   = bits[7]         # A
        self.num_sat      = int(bits[8])    # 8
        self.gsm_signal   = int(bits[9])    # 18
        self.speed        = int(bits[10])   # 0 km/h
        self.direction    = int(bits[11])   # 193
        self.hor_acc      = float(bits[12]) # 1.6
        self.alt          = int(bits[13])   # 78 m
        self.mileage      = int(bits[14])   # 24 m
        self.run_time     = int(bits[15])   # 38067
        self.base_stat    = bits[16]        # 240|24|2FA9|0B9C
        self.port_stat    = bits[17]        # 0000
        self.analog_in    = bits[18]        # 0002|0002|0000|097E|0000  #battery?
        #bits[19] ?                         # 00000001 ?? depends on msg code?
        #bits[20]                           # empty

        # last two characters are checksum
        self.checksum = "0x"+crc(msg[0:-2])
        if self.checksum != "0x"+msg[-2:]:
            self.error( "No checksum found." )
            pass

        analog_bits = self.analog_in.split('|')
        if len(analog_bits) == 5:
            batt = round((int(analog_bits[3], 16)*6.6)/4096.0, 2)
            
        if self.evt_code in [34, 35]:
            self.set_pos()
            dd = round(self.dist()[0]*1000,0)
            mi = round(self.mileage/1000.0,3)
            #rt = str(datetime.timedelta(seconds=self.run_time))
            self.info(self.datetime_str+" "+str(self.pos)+" "+str(dd)+" "+str(mi)+" "+str(batt)+"v") #+" {"+rt+"}")
            
        self.msg_cnt += 1
        
    def set_pos(self):
        self.prev_pos = self.pos #improve (ringbuffer?)
        if self.lat and self.lon:
            self.pos = Pos(self.imei,self.lat,self.lon,self.speed,self.direction,self.hor_acc,self.alt,self.msg_cnt)
        else:
            self.pos = None
    def dist(self): #if we have prev_pos
        if not self.prev_pos:
            return (0, 0)
        R = 6378.1 # km
        dlat = math.radians(self.prev_pos.lat-self.pos.lat)
        dlon = math.radians(self.prev_pos.lon-self.pos.lon)
        a = math.sin(dlat/2) * math.sin(dlat/2) + math.cos(math.radians(self.pos.lat)) * math.cos(math.radians(self.prev_pos.lat)) * math.sin(dlon/2) * math.sin(dlon/2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        d = R * c
        y = math.sin(dlon) * math.cos(math.radians(self.prev_pos.lat))
        x = math.cos(math.radians(self.pos.lat))*math.sin(math.radians(self.prev_pos.lat)) - math.sin(math.radians(self.pos.lat))*math.cos(math.radians(self.prev_pos.lat))*math.cos(dlon)
        brng = math.degrees(math.atan2(y, x)) % 360
        return (d, brng)
    def make_A21(self,ip):
        #make msg etc, set expected to ...
        pass
    
class TK106RequestHandler(socketserver.BaseRequestHandler):
    def __init__(self, request, client_address, server):
        self.logger = glogger #logging.getLogger('TK106Handler')
        self.logger.debug('New request from '+repr(client_address))
        socketserver.BaseRequestHandler.__init__(self, request, client_address, server)
        return

    def debug(self, msg):
        self.logger.debug("[%s] "+str(msg), self.snr+"/"+str(self.imei)[0:8])

    def info(self, msg):
        self.logger.info("[%s] "+str(msg), self.snr+"/"+str(self.imei)[0:8])

    def error(self, msg):
        self.logger.error("[%s] "+str(msg), self.snr+"/"+str(self.imei)[0:8])

    def on_start(self):
        """
        Called when a new tracker is initialised.
        """
        # read r.queue file here?
        # Fill in required __IMEI__
        item = (4, (__IMEI__, "A10")) #A10 is position report, long interval takes long time to start
        #r.put(item)
        item = (20, (__IMEI__, "B05,1,53.069748,12.402491,1000,0,1") ) #geofence coordinates
        #r.put(item)
        if self.poshandler:
            self.poshandler.on_start()

    def on_finish(self):
        """
        Called before the thread exits.
        """
        if self.poshandler:
            self.poshandler.on_finish()

    def on_msg(self, msg):
        if self.poshandler:
            self.poshandler.on_msg(str(msg))
        # if self.protocol.evt_code == FENCE, send SMS, or something

    def on_position(self):
        """
        Called everytime we receive a GPS position string.
        """
        if self.poshandler:
            self.poshandler.on_position()

    def on_stationary(self):
        """
        Called if last two positions are the same. To be implemented.
        """
        if self.poshandler:
            self.poshandler.on_stationary()

    def on_start_move(self):
        """
        Called if moving again after stationary destection. To be implemented.
        """
        if self.poshandler:
            self.poshandler.on_start_move()

    def send(self, msg):
        self.debug('send: '+msg)
        msg_bytes = bytes(msg+"\r\n", "utf8")
        self.request.send(msg_bytes)
        self.bytes_s += len(msg_bytes)

    def handle(self):
        global lock
        self.request.settimeout(thread_tout_div)
        
        cur_pid         = serial() #os.getpid() #threading.current_thread() 
        self.logger.info("TK106RequestHandler handle start, pid: "+str(cur_pid))
        self.loop       = True
        self.start      = time.time() # local running time
        self.last       = time.time()
        self.lastrcvd   = time.time()
        self.snr        = str(cur_pid)
        self.imei       = self.snr    #until we get the real one (include "PID:"?)
        self.url        = ""
        self.ctldir     = "tk106nr_"+self.snr #still needed?
        self.lastfile   = self.ctldir+"/last"  #touched, for timestamp
        self.cmdfile    = self.ctldir+"/cmd"   #reads a command from this file if it exists DEPRECATED
        self.xyzzyfile  = self.ctldir+"/xyzzy" #instead of pickled info
        self.imeifile   = self.ctldir+"/imei"  #contains imei nr
        self.rawfile    = self.ctldir+"/raw"   #contains last string received
        self.bytesfile  = self.ctldir+"/bytes" #contains bytes received/sent (pickled)
        self.exitfile   = self.ctldir+"/exit"  #written on tracker exit/disappearance
        self.histfile   = self.ctldir+"/hist"  #written on tracker exit/disappearance, history
        self.lat        = 0
        self.lon        = 0
        self.spd        = 0
        self.bearing    = 0
        self.acc        = 0
        self.pos        = None
        self.bytes_r    = 0
        self.bytes_s    = 0
        self.mile_s     = 0 #mileage start
        self.mile_e     = 0 #mileage end
        self.posidx     = 0 # number of positions received
        self.poshandler = None
        self.protocol   = NOPROT(self.logger, self.imei) # = None

        # create control dir
        try:
            if not os.path.exists(self.ctldir):
                os.mkdir(self.ctldir)
                self.debug("created "+self.ctldir)
            else:
                self.info("ctldir existed.")
        except:
            self.logger.error("[%s] Could not create dir ["+self.ctldir+"]", str(self.imei))
            self.loop = False
        #create last file
        try:
            fp_last = open(self.lastfile, 'w')
            fp_last.write(str(cur_pid))
            fp_last.close()
        except:
            self.error("Could not create 'last' file.")
            self.loop = False
        #
        # LOOP
        #
        global info
        info.add_serial(self.snr)
        while self.loop:
            sdata = ""
            try:
                sdata = self.request.recv(4096)
                if not sdata:
                    self.error("Not data.") #why does this happen? Broken connexion?
                    time.sleep(0.1) #OS X bug workaround
                    self.loop = False
                    continue
            except timeout:
                #self.error("Timeout")
                #pass #it will continue with an () emtpy line
                pass
                # and it can be used to check for a cmd...
            except socket.error as err:
                if str(err) == "[Errno 35] Resource temporarily unavailable":
                    time.sleep(0.1) #OS X bug workaround
                    continue
            except KeyboardInterrupt:
                self.error("KEYBOARD")
                sys.exit(0)
            except:
                self.error("Socket error.")
            
            if sdata != "":
                self.bytes_r += len(sdata)
                sdata = sdata.rstrip()
                self.debug("recv("+str(sdata)+")") #python3, added str()
                # Save last received data in file
                with open(self.rawfile, "wb") as f:
                    f.write(sdata)
                self.lastrcvd = time.time()
                
            # If '\n' in data more than once we have more lines.
            lc = 0
            for data in b2s(sdata).split("\n"): #sdata.split('\n'):
                data = data.rstrip()
                if data:
                    self.info("line["+str(lc)+"]: ("+data+")")
                    if data == "__EXIT__": #these bytes are not counted
                        self.loop = False
                        continue
                lc += 1

                # we also need an expected answer, if we have sent a command
                
                # TK106
                # $$R154,0ximei,AAA,35,53.069868,12.402848,151112085528,A,8,18,0,193,1.6,78,24,38067,240|24|2FA9|0B9C,0000,0002|0002|0000|097E|0000,00000001,,1,0000*E3
                if data[0:2] == "$$": # MESSAGE, comes every time like this
                    #how to determine first one?
                    if self.imei == self.snr: #check protocol or something?
                        m= re.match('\$\$.\d+?,(\d+),', data)
                        if m:
                            self.imei = m.group(1) #now it is a string
                            self.logger = glogger
                            self.info("Init: imei "+self.imei)
                            # here we CHANGE protocols? save NOPROT history?
                            old_history = self.protocol.history.get()
                            self.protocol = TK106(self.logger, self.imei)
                            for l in old_history:
                                self.protocol.history.append(l)
                            # create imei file
                            try:
                                fp_imei = open(self.imeifile, 'w')
                                fp_imei.write(self.imei)
                                fp_imei.close()
                            except:
                                self.error("Could not create 'imei' file")
                                self.loop = False
                                continue
                            # store
                            info.add_imei(str(self.snr)+"/"+str(self.imei))
                            # check if we want to send a message back?
                        else:
                            self.info("No IMEI?")
                            continue #loop to false?
                        # still handling from PID to IMEI
                        self.last = time.time()
                        self.poshandler = POSHandler( self ) 
                        self.on_start()
                        #self.on_msg() # or event?
                        
                    self.last = time.time()
                    
                    # handle msg
                    # handle_tk106(data) #self.protocol_handler(...) ? or that are we already self

                    if not self.protocol:
                        continue

                    # crc, here or in protocol?
                    if len(data) > 4:
                        if data[-3] == '*':
                            checksum = "0x"+crc(data[0:-2]) #is that right slice?
                            #is it still hex?
                            if checksum != "0x"+data[-2:]:
                                self.info("Bad checksum, "+str(checksum)+" is not 0x"+str(data[-2:]))
                                continue

                    self.protocol.parse(data)
                    if self.mile_s == 0:
                        self.mile_s = self.protocol.mileage
                    self.mile_e = self.protocol.mileage         # maybe update mileage in Info()
                    #self.info( self.protocol.checksum )

                    #touch (maybe earlier)
                    os.utime(self.lastfile, None)

                    if self.protocol.pos:
                        self.pos = self.protocol.pos
                        self.on_position()
                        self.posidx += 1

                        time.sleep(.1)
                
                # Check Queue (before timeout check?)
                #
                #print(r.qsize(), repr(r), repr(r.empty()) )
                #print( "loop", repr(self.server._pq), repr(self.server._pq.qsize()) )
                if not self.server._pq.empty():
                    #self.info("lock.acquire()")
                    if lock.acquire(True,1.0):
                        item = self.server._pq.get() # ( stamp, (imei, cmd) )
                        # stamp some local (thread) running time?
                        #if stamp > time.time - thread_start_time: ...
                        item_imei = item[1][0] # (5743542542.34, (46784673648372, "A12,10\r\n") )
                        #self.info( "item_imei="+str(item_imei)+", item_snr="+str(self.snr) )
                        if int(item_imei) == int(self.imei) or int(item_imei) == int(self.snr): #snr for local comms
                            item_stamp = float(item[0])
                            #self.info( "item_stamp="+str(item_stamp) )
                            if time.time() > item_stamp: #not start, from when was put on pq?
                                #self.debug(repr(item))
                                item_cmd = item[1][1]
                                #self.info("item_cmd="+item_cmd)
                                if item_cmd == "END":
                                    self.loop = False
                                else:
                                    self.do_cmd(item_cmd)
                            else:
                                self.server._pq.put(item) #not time yet
                        else:
                            self.server._pq.put(item) #was wrong imei
                        lock.release()
                        #self.info("lock.release()")
                    else:
                        self.info("no lock")

                # Write receive/send stats
                with open(self.bytesfile, "wb") as f:
                    pickle.dump((self.bytes_r, self.bytes_s, self.posidx), f)

                with open(self.xyzzyfile, "w") as f:
                    f.write( str(self.snr)+","+str(self.imei)+","
                             +str(self.bytes_r)+","+str(self.bytes_s)+","+str(self.posidx)+","
                             +str(self.mile_s)+","+str(self.mile_e)
                             +"\n" )
                
                # no_data received for thread_tout
                # PJB: MAYBE send something once to get going again?
                no_data = float(time.time() - self.lastrcvd)
                if no_data > thread_tout:
                    self.info("No data for %.1f seconds, timeout." % (no_data))
                    self.loop = False
                    # do an on_timeout cmd? check queue for "-1" (or leftover) stamp items?

        self.info('handle ready')
        info.del_imei(str(self.snr)+"/"+str(self.imei))
        info.del_serial(str(self.snr))
        if self.protocol:
            with open(self.histfile, "w") as f: #https://pymotw.com/2/bz2/
                for l in self.protocol.history.get():
                    #print(l)
                    f.write(str(l)+"\n")
        m = (float(self.mile_e) - float(self.mile_s)) / 1000.0
        glogger.info( "MILEAGE: %.3f" % m )
        return

    def do_cmd(self, cmd):
        if cmd[0:3] == 'A10': #pos report
            cmd = msg_A10(self.imei)
            self.info(cmd)
            self.send(cmd)
        elif cmd[0:3] == 'A12':
            params = cmd[4:].split(",")
            #self.info(params)
            try:
                cmd = msg_A12(self.imei, params[0])
                self.info(cmd)
                self.send(cmd) #self.send() which calls self.request.send?
            except IndexError:
                self.error("Could not handle A12 'cmd' file.")
        elif cmd[0:3] == 'A21': # 'A21 1,78.12.34.45,9100,services.telenor.se', split?
            # 0 0 A21,1,78.69.184.128,9030,services.telenor.se
            #         0 1             2    3   APN-user and APN-pass are coded in msg_A21
            params = cmd[4:].split(",")
            self.info(params)
            try:
                cmd = msg_A21(self.imei, params[0], params[1], params[2], params[3])
                self.info(cmd)
                self.send(cmd) #self.send() which calls self.request.send?
            except IndexError:
                self.error("Could not handle A21 'cmd' file.")
            self.info("1234")
        elif cmd[0:3] == 'A23': # 'A23,78.69.179.212,9300'
            params = cmd[4:].split(",")
            self.info(params)
            try:
                cmd = msg_A23(self.imei, params[0], params[1])
                self.info(cmd)
                self.send(cmd) #self.send() which calls self.request.send?
            except IndexError:
                self.error("Could not handle A21 'cmd' file.")
        elif cmd[0:3] == 'A70':
            cmd = msg_A70(self.imei)
            self.info(cmd)
            self.send(cmd)
        elif cmd[0:3] == "B05":
            # msg_B05(imei, fnum, lat, lon, rad, alrm_in, alrm_out)
            params = cmd[4:].split(",")
            self.info(params)
            try:
                cmd = msg_B05(self.imei, params[0], params[1], params[2], params[3], params[4], params[5])
                self.info(cmd)
                self.send(cmd) #self.send() which calls self.request.send?
            except IndexError:
                self.error("Could not handle B05 'cmd' file.")
        elif cmd[0:3] == "F08":
            params = cmd[4:].split(",")
            self.info(params)
            try:
                cmd = msg_F08(self.imei, params[0], params[1]) # could be "" to "ignore"
                self.info(cmd)
                self.send(cmd) #self.send() which calls self.request.send?
            except IndexError:
                self.error("Could not handle F08 'cmd' file.")
        #A21,Connection mode,IP address,   Port,APN,APN user name,APN password
        #A21,              1,255.255.255.255,9030,telenor?,,
    def finish(self):
        self.info('handle finish')
        self.on_finish()
        with open(self.exitfile, "w") as f:
            f.write( repr(self.imei)+"\n" )
        self.info( "bytes read="+str(self.bytes_r)+" bytes sent="+str(self.bytes_s)+" points="+str(self.posidx) )
        return socketserver.BaseRequestHandler.finish(self)

#socketserver.ThreadingMixIn socketserver.ForkingMixIn
class TK106Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    timeout             = 10
    daemon_threads      = True
    allow_reuse_address = True

def determine_td(d):
    """
    Difference between ctime of imei file (on creation) and the last
    access time of the last file is the total running time.
    """
    td0 = 0
    td1 = 0
    if os.path.exists(d+"/last"):
        td0 = int(float(os.stat(d+"/last").st_atime))
    if os.path.exists(d+"/imei"):
        td1 = int(float(os.stat(d+"/imei").st_ctime))
    if td0*td1 == 0:
        return 0
    return abs(td1-td0)

def on_exit(imei, msg=""):
    """
    Called when the tread is killed/tracker disappeared.
    """
    ph = POSHandler( None )
    ph.on_exit(imei, msg)

def b2s(b):
    return "".join(map(chr, b))

def msg_A21(imei, conn_mode, ip, port, apn):
    '''
    GPRS Sending
    @@H48,35335801778xxxx,A21,1,67.203.13.26,8800,,,*C9
    GPRS Reply
    $$H28,35335801778xx,A21,OK*F4\r\n   <-- we need an expected answer string
    '''
    msg_part = str(imei)+",A21,"+str(conn_mode)+","+str(ip)+","+str(port)+","+str(apn)+",,*"
    msg = "@@A"+str(len(msg_part)+5)+","+msg_part
    #str(imei)+",A21,"+str(conn_mode)+","+str(ip)+","+str(port)+","+str(apn)+",,*"
    chs = crc( msg )
    msg += chs #+ "\r\n"
    return msg

def msg_A23(imei, ip, port):
    '''
    GPRS Sending
    @@H48,35335801778xxxx,A21,1,67.203.13.26,8800,,,*C9
    GPRS Reply
    $$H28,35335801778xxxx,A21,OK*F4\r\n   <-- we need an expected answer string
    '''
    msg_part = str(imei)+",A23,"+str(ip)+","+str(port)+"*"
    msg = "@@A"+str(len(msg_part)+5)+","+msg_part
    chs = crc( msg )
    msg += chs #+ "\r\n"
    return msg

def msg_A12(imei, iv):
    '''
    '''
    msg_part = str(imei)+",A12,"+str(iv)+"*"
    msg = "@@A"+str(len(msg_part)+5)+","+msg_part
    chs = crc( msg )
    msg += chs #+ "\r\n"
    return msg

def msg_A10(imei):
    '''
    @@Q25,35335801778xxxx,A10*6A\r\n
    '''
    msg_part = str(imei)+",A10*"
    msg = "@@A"+str(len(msg_part)+5)+","+msg_part
    chs = crc( msg )
    msg += chs #+ "\r\n"
    return msg

def msg_A70(imei):
    '''
    @@T25,35335801778xxxx,A70*93\r\n
    '''
    msg_part = str(imei)+",A70*"
    msg = "@@A"+str(len(msg_part)+5)+","+msg_part
    chs = crc( msg )
    msg += chs #+ "\r\n"
    return msg

def msg_B05(imei, fnum, lat, lon, rad, alrm_in, alrm_out):
    '''
    @@H57,35335801778xxxx,B05,1,22.913191,114.079882,1000,0,1*96\r\n

    20151118 15:38:35 [86677102012xxxx] (20, (86677102012xxxx, 'B05,1,53.069748,12.402491,1000,0,1'))
    20151118 15:38:35 [86677102012xxxx] ['1', '56.069748', '13.402491', '1000', '0', '1']
    20151118 15:38:35 [86677102012xxxx] @@A56,86677102012xxxx,B05,1,52.069748,11.402491,1000,0,1*56
    20151118 15:38:37 [86677102012xxxx] line[0]: ($$A28,86677102012xxxx,B05,OK*E7)
    '''
    msg_part = "%s,B05,%i,%.6f,%.6f,%i,%i,%i*" % (imei, int(fnum), float(lat), float(lon), int(rad), int(alrm_in), int(alrm_out))
    msg = "@@A"+str(len(msg_part)+5)+","+msg_part
    chs = crc( msg )
    msg += chs #+ "\r\n"
    return msg

def msg_F08(imei, secs, dist):
    '''
    F08,Run time,Mileage
    '''
    msg_part = str(imei)+",F08,"+str(secs)+","+str(dist)+"*"
    msg = "@@A"+str(len(msg_part)+5)+","+msg_part
    chs = crc( msg )
    msg += chs #+ "\r\n"
    return msg

def crc(msg):
    '''
    1. Find the ASCII code of all the characters from the first @ all the way to the * before the checksum
    2. Add all the ASCII codes together and convert to hex
    3. Take the two rightmost digits of the hex number, and that will be your checksum
    '''
    sum = 0
    #b = str.encode(msg)
    b = msg
    for x in b:
        #sum += x
        sum += ord(x)
    #print( "{:02x}".format( sum ) )
    return "{:02x}".format( sum % 256 ).upper()

def serial():
    global n
    while os.path.exists("tk106nr_"+str(n)):
        n += 1
    return n

if __name__ == '__main__':
    r = queue.PriorityQueue() # -> (timestamp, (imei, cmd))
    lock = threading.Lock()
    n = 1000 #start directories with this serial number
    info = Info() #{ "imeis":[] } #global info
    
    # Status rep. after 30 seconds
    '''
    item = (30, (12, "A10")) #A10 is position report
    r.put(item)

    # Setting geofence easier with SMS? Or sms replies sms, grps replies gprs?
    item = (20, (86677102012xxxx, "B05,1,51.069748,11.402491,1000,0,1") )
    r.put(item)
    B05,2,53.39840241,4.74052811,100,1,1
    '''
    
    # Default settings
    clean_dirs      = False #remove the old directories
    list_dirs       = False 
    port            = 9030
    thread_tout     =  300 #timeout in the thread handling the connexion
    thread_tout_div =    1 #thread timeout
    watcher_port    = 0 #None #0 means port+1
    
    try:
        opts, args = getopt.getopt(sys.argv[1:], "cd:ln:p:t:w:", [])
    except getopt.GetoptError as err:
        print(str(err))
        sys.exit(1)
    for o, a in opts:
        if o in ("-c"):
            clean_dirs = True
        elif o in ("-d"):
            thread_tout_div = int(a)
        elif o in ("-l"):
            list_dirs = True
        elif o in ("-p"):
            port = int(a)
        elif o in ("-n"):
            n = int(a)
        elif o in ("-t"):
            thread_tout = int(a)
        elif o in ("-w"):
            watcher_port = int(a)
        else:
            assert False, "unhandled option"

    if watcher_port == 0:
        watcher_port = port + 1
        
    if list_dirs:
        #dirs = sorted( os.listdir(".") )
        dirs = sorted([x for x in os.listdir(".") if x.startswith("tk106nr_")])
        #print( dirs[-1].split('_')[1] )
        for d in dirs:
            if d[0:8] == "tk106nr_":
                glogger.info("-----> DIR: "+d)
                td = determine_td(d)
                if td:
                    glogger.info("Time delta: %s", str(datetime.timedelta(seconds=td)))
                else:
                    pass
                    #glogger.info("No time info.")
                with open(d+"/bytes", "rb") as f:
                    (r,s,p) = pickle.load(f)
                    glogger.info( "bytes read="+str(r)+" bytes sent="+str(s)+" points="+str(p) )
                if os.path.exists(d+"/exit"):
                    glogger.info( "Exit file exists.")
                if os.path.exists(d+"/hist"):
                    glogger.info( "History file exists.")
        sys.exit(0)

    # Remove left over directories form last time.
    if clean_dirs:
        dirs = os.listdir(".")
        for d in dirs:
            if d[0:8] == "tk106nr_":
                glogger.info("RMDIR: "+d)
                td = determine_td(d)
                glogger.info("Time delta: %s", str(datetime.timedelta(seconds=td)))
                # check if /exit is there? or another file?
                with open(d+"/bytes", "rb") as f:
                    (r,s,p) = pickle.load(f)
                    glogger.info( "bytes read="+str(r)+" bytes sent="+str(s)+" points="+str(p) )
                shutil.rmtree(d)

    address  = ('', port) 
    server   = TK106Server(address, TK106RequestHandler)
    ip, port = server.server_address
    server._pq = r
    t = threading.Thread(target=server.serve_forever)
    t.setDaemon(True) # don't hang on exit
    t.start()
    glogger.info("Server loop running in process:"+str(os.getpid())+" on port:"+str(port))
    glogger.info("Timeout: "+str(thread_tout)+"/"+str(thread_tout_div))

    if watcher_port:
        glogger.info("Watcher running on port:"+str(watcher_port))
        watcher = socketserver.TCPServer(('localhost', watcher_port), MyWatcher) #or on ''?
        watcher._pq = server._pq
        #watcher.serve_forever()
        w = threading.Thread(target=watcher.serve_forever)
        w.setDaemon(True) 
        w.start()

    #d = datetime.datetime(2015,11,18,10,10,46) # UTC time
    #print( calendar.timegm(d.timetuple()) )    # to unix epoch
    #
    
    # This loops checks the directories, and if there is a timeout, kills subprocess if necessary
    x = len(info.imeis)
    sunshine = True
    while sunshine:
        try:
            time.sleep(1)
            if len(info.imeis) != x:
                x = len(info.imeis)
                glogger.info(repr(info.imeis))
            for i in info.imeis:
                nr, im = i.split('/')
                d = "tk106nr_"+str(nr)
                #td = determine_td(d)
                if os.path.exists(d+"/last"): #not live
                    # time between now and last sign of life:
                    td = int(time.time()-float(os.stat(d+"/last").st_atime))
                    if td > 600: #2 x normal timeout
                        glogger.info( "MAYBE DEAD, RESTARTING!" )
                        # should cleanly exit, somehow...
                        # and what about if tracking more than one tracker?
                        # os.execl(sys.executable, sys.executable, *sys.argv)
                        # os.execv(sys.executable, [sys.executable] + sys.argv)
                        os.execl(sys.executable, 'python3', __file__, *sys.argv[1:])
        except KeyboardInterrupt:
            glogger.info("KeyboardInterrupt")
            sunshine = False
# no more sunshine
print( info.serials )
for i in info.serials:
    snr = int(i) # if using imeis -> i.split('/')[0])
    item = (0, (snr, "END"))
    print( item )
    # lock?
    r.put(item)
tries = 10
while info.serials and --tries > 0:
    time.sleep(1)
print("ready")
    
