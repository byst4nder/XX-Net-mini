import queue
import operator
import os
import sys
import threading
import time
import random


class IpManagerBase():
    def __init__(self, config, ip_source, logger):
        self.scan_thread_lock = threading.Lock()
        self.ip_lock = threading.Lock()
        
        self.config = config
        self.ip_source = ip_source
        self.logger = logger
        self.ips = []

    def load_config(self):
        pass

    def set_ips(self, ips):
        self.ips = ips

    def get_ip(self):
        if not self.ips:
            return ""
        return random.choice(self.ips)

    def update_ip(self, ip_str, handshake_time):
        pass

    def report_connect_fail(self, ip_str, reason=""):
        pass

    def report_connect_closed(self, ip_str, reason=""):
        pass

    def ssl_closed(self, ip_str, reason=""):
        pass
    

######################################
# about ip connect time and handshake time
# handshake time is double of connect time in common case.
# after connect and handshaked, http get time is like connect time
#
# connect time is zero if you use socks proxy.
#
# most case, connect time is 300ms - 600ms.
# good case is 60ms
# bad case is 1300ms and more.

class IpManager():
    # Functions:
    # 1. Scan ip in back ground
    # 2. sort ip by RTT and fail times
    #     RTT + fail_times * 1000
    # 3. count ip connection number
    #    keep max one link every ip.
    #    more link may be block by GFW if large traffic on some ip.
    # 4. scan all exist ip
    #    stop scan ip thread then start 10 threads to scan all exist ip.
    #    called by web_control.

    def __init__(self, logger, config,  check_local_network, default_ip_list_fn, ip_list_fn, scan_ip_log=None):
        self.logger = logger
        self.config = config        
        self.check_local_network = check_local_network
        # self.check_ip = check_ip
        self.scan_ip_log = scan_ip_log

        self.default_ip_list_fn = default_ip_list_fn
        self.ip_list_fn = ip_list_fn

        self.scan_thread_lock = threading.Lock()
        self.ip_lock = threading.Lock()
        self.reset()

        # self.check_ip_thread = threading.Thread(target=self.check_ip_process)
        # self.check_ip_thread.daemon = True
        # self.check_ip_thread.start()


    def reset(self):
        self.ip_lock.acquire()
        self.ip_pointer = 0
        self.ip_pointer_reset_time = 0
        self.scan_thread_count = 0
        self.scan_fail_count = 0
        self.scan_recheck_interval = 3
        self.iplist_need_save = False
        self.iplist_saved_time = 0
        self.last_sort_time = 0 # keep status for avoid wast too many cpu
        self.good_ip_num = 0 # only success ip num
        self.good_ipv4_num = 0
        self.good_ipv6_num = 0
        self.running = True

        # ip_str => {
                 # 'handshake_time'=>?ms,
                 # 'links' => current link number, limit max to 1
                 # 'fail_times' => N   continue timeout num, if connect success, reset to 0
                 # 'fail_time' => time.time(),  last fail time, next time retry will need more time.
                 # 'transfered_data' => X bytes
                 # 'down_fail' => times of fails when download content data
                 # 'down_fail_time'
                 # 'data_active' => transfered_data - n second, for select
                 # 'get_time' => ip used time.
                 # 'success_time' => last connect success time.
                 # 'domain'=>CN,
                 # 'server'=>gws/gvs?,
                 # history=>[[time,status], []]
                 # }

        # ip_str can be ip or ip:port stirng.

        self.ip_dict = {}

        # gererate from ip_dict, sort by handshake_time, when get_batch_ip
        self.ip_list = []
        self.to_check_ip_queue = queue.Queue()
        self.scan_exist_ip_queue = queue.Queue()
        self.ip_lock.release()

        self.load_config()
        self.load_ip()

        #if check_local_network.network_stat == "OK" and not config.USE_IPV6:
        #    self.start_scan_all_exist_ip()
        # self.search_more_ip()

    def is_ip_enough(self):
        if len(self.ip_list) >= self.max_good_ip_num:
            return True
        else:
            return False

    def load_config(self):
        self.scan_ip_thread_num = self.config.max_scan_ip_thread_num
        self.max_links_per_ip = self.config.max_links_per_ip
        self.max_good_ip_num = self.config.max_good_ip_num #3000  # stop scan ip when enough
        self.auto_adjust_scan_ip_pointer = int(30 + self.max_good_ip_num * 0.1)
        self.ip_connect_interval = self.config.ip_connect_interval #5,10
        self.record_ip_history = self.config.record_ip_history

    def load_ip(self):
        if os.path.isfile(self.ip_list_fn):
            file_path = self.ip_list_fn
        elif self.default_ip_list_fn and os.path.isfile(self.default_ip_list_fn):
            file_path = self.default_ip_list_fn
        else:
            return

        with open(file_path, "r") as fd:
            lines = fd.readlines()

        for line in lines:
            try:
                if line.startswith("#"):
                    continue

                str_l = line.split(' ')

                if len(str_l) < 4:
                    self.logger.warning("line err: %s", line)
                    continue
                ip_str = str_l[0]
                domain = str_l[1]
                server = str_l[2]
                handshake_time = int(str_l[3])
                if len(str_l) > 4:
                    fail_times = int(str_l[4])
                else:
                    fail_times = 0

                if len(str_l) > 5:
                    down_fail = int(str_l[5])
                else:
                    down_fail = 0

                #self.logger.info("load ip: %s time:%d domain:%s server:%s", ip, handshake_time, domain, server)
                self.add_ip(ip_str, handshake_time, domain, server, fail_times, down_fail, False)
            except Exception as e:
                self.logger.exception("load_ip line:%s err:%s", line, e)

        self.logger.info("load ip_list num:%d, target num:%d", len(self.ip_dict), len(self.ip_list))
        self.try_sort_ip(force=True)
        # if file_path == self.default_good_ip_file:
        #    self.logger.info("first run, rescan all exist ip")
        #    self.start_scan_all_exist_ip()

    def save(self, force=False):
        if not force:
            if not self.iplist_need_save:
                return
            if time.time() - self.iplist_saved_time < 10:
                return

        self.iplist_saved_time = time.time()

        try:
            self.ip_lock.acquire()
            ip_dict = sorted(list(self.ip_dict.items()),  key=lambda x: (x[1]['handshake_time'] + x[1]['fail_times'] * 1000))
            with open(self.ip_list_fn, "w") as fd:
                for ip_str, property in ip_dict:
                    fd.write( "%s %s %s %d %d %d\n" %
                        (ip_str, property['domain'],
                            property['server'],
                            property['handshake_time'],
                            property['fail_times'],
                            property['down_fail']) )
                fd.flush()

            self.iplist_need_save = False
        except Exception as e:
            self.logger.error("save %s fail %s", self.ip_list_fn, e)
        finally:
            self.ip_lock.release()

    def _ip_rate(self, ip_info):
        return ip_info['handshake_time'] + \
                    (ip_info['fail_times'] * 500 ) + \
                    (ip_info['down_fail'] * 500 )

    def _add_ip_num(self, ip_str, num):
        if "." in ip_str:
            self.good_ipv4_num += num
        else:
            self.good_ipv6_num += num
        self.good_ip_num += num

    def try_sort_ip(self, force=False):
        if time.time() - self.last_sort_time < 10 and not force:
            return

        self.ip_lock.acquire()
        self.last_sort_time = time.time()
        try:
            self.good_ip_num = 0
            self.good_ipv4_num = 0
            self.good_ipv6_num = 0
            ip_rate = {}
            for ip_str in self.ip_dict:
                if "." in ip_str and self.config.use_ipv6 == "force_ipv6":
                    continue

                if not "." in ip_str and self.config.use_ipv6 == "force_ipv4":
                    continue

                if 'gws' not in self.ip_dict[ip_str]['server']:
                    continue
                ip_rate[ip_str] = self._ip_rate(self.ip_dict[ip_str])
                if self.ip_dict[ip_str]['fail_times'] == 0:
                    self._add_ip_num(ip_str, 1)

            ip_time = sorted(list(ip_rate.items()), key=operator.itemgetter(1))
            self.ip_list = [ip_str for ip_str, rate in ip_time]

        except Exception as e:
            self.logger.error("try_sort_ip_by_handshake_time:%s", e)
        finally:
            self.ip_lock.release()

        time_cost = ((time.time() - self.last_sort_time) * 1000)
        if time_cost > 30:
            self.logger.debug("sort ip time:%dms", time_cost) # 5ms for 1000 ip. 70~150ms for 30000 ip.

        self.adjust_scan_thread_num()

    def adjust_scan_thread_num(self):
        ip_num = len(self.ip_list)
        min_scan_ip_thread_num = 1 if self.config.max_scan_ip_thread_num else 0

        if not self.config.auto_adjust_scan_ip_thread_num:
            scan_ip_thread_num = self.config.max_scan_ip_thread_num
        elif ip_num < self.max_good_ip_num:
            scan_ip_thread_num = int(self.config.max_scan_ip_thread_num * (1.5 - ip_num / self.max_good_ip_num))
        else:
            try:
                if ip_num > self.auto_adjust_scan_ip_pointer:
                    last_ip = self.ip_list[self.auto_adjust_scan_ip_pointer]
                else:
                    last_ip = self.ip_list[-1]

                last_ip_handshake_time = self._ip_rate(self.ip_dict[last_ip])
                scan_ip_thread_num = int((last_ip_handshake_time - self.config.target_handshake_time) / 2 * \
                                          self.config.max_scan_ip_thread_num / 50 * \
                                          self.max_good_ip_num / max(self.good_ip_num, 1))
            except Exception as e:
                self.logger.warn("adjust_scan_thread_num fail:%r", e)
                return

        if scan_ip_thread_num > self.config.max_scan_ip_thread_num:
            scan_ip_thread_num = self.config.max_scan_ip_thread_num
        elif scan_ip_thread_num < min_scan_ip_thread_num:
            scan_ip_thread_num = min_scan_ip_thread_num

        if scan_ip_thread_num != self.scan_ip_thread_num:
            self.logger.info("Adjust scan thread num from %d to %d", self.scan_ip_thread_num, scan_ip_thread_num)
            self.scan_ip_thread_num = scan_ip_thread_num
            # self.search_more_ip()

    def ip_quality(self, num=10):
        try:
            iplist_length = len(self.ip_list)
            ip_th = min(num, iplist_length)
            for i in range(ip_th, 0, -1):
                last_ip = self.ip_list[i]
                if self.ip_dict[last_ip]['fail_times'] > 0:
                    continue
                handshake_time = self.ip_dict[last_ip]['handshake_time']
                return handshake_time

            return 9999
        except:
            return 9999

    def append_ip_history(self, ip_str, info):
        if self.record_ip_history:
            self.ip_dict[ip_str]['history'].append([time.time(), info])

    # algorithm to get ip:
    # scan start from fastest ip
    # always use the fastest ip.
    # if the ip is used in 5 seconds, try next ip;
    # if the ip is fail in 60 seconds, try next ip;
    # reset pointer to front every 3 seconds
    def get_ip(self, to_recheck=False):
        if not to_recheck:
            self.try_sort_ip()

        self.ip_lock.acquire()
        try:
            ip_num = len(self.ip_list)
            if ip_num == 0:
                #self.logger.warning("no ip")
                time.sleep(1)
                return None

            ip_connect_interval = ip_num * self.scan_recheck_interval + 200 if to_recheck else self.ip_connect_interval

            for i in range(ip_num):
                time_now = time.time()
                if self.ip_pointer >= ip_num:
                    if time_now - self.ip_pointer_reset_time < 1:
                        time.sleep(1)
                        continue
                    else:
                        self.ip_pointer = 0
                        self.ip_pointer_reset_time = time_now
                elif self.ip_pointer > 0 and time_now - self.ip_pointer_reset_time > 3:
                    self.ip_pointer = 0
                    self.ip_pointer_reset_time = time_now

                ip_str = self.ip_list[self.ip_pointer]
                if "." in ip_str and self.config.use_ipv6 == "force_ipv6":
                    continue

                if not "." in ip_str and self.config.use_ipv6 == "force_ipv4":
                    continue

                get_time = self.ip_dict[ip_str]["get_time"]
                if time_now - get_time < ip_connect_interval:
                    self.ip_pointer += 1
                    continue

                if not to_recheck:
                    if time_now - self.ip_dict[ip_str]['success_time'] > self.config.long_fail_threshold: # 5 min
                        fail_connect_interval = self.config.long_fail_connect_interval # 180
                    else:
                        fail_connect_interval = self.config.short_fail_connect_interval # 10
                    fail_time = self.ip_dict[ip_str]["fail_time"]
                    if time_now - fail_time < fail_connect_interval:
                        self.ip_pointer += 1
                        continue

                    down_fail_time = self.ip_dict[ip_str]["down_fail_time"]
                    if time_now - down_fail_time < self.config.down_fail_connect_interval:
                        self.ip_pointer += 1
                        continue

                if self.ip_dict[ip_str]['links'] >= self.max_links_per_ip:
                    self.ip_pointer += 1
                    continue

                handshake_time = self.ip_dict[ip_str]["handshake_time"]
                # self.logger.debug("get ip:%s t:%d", ip, handshake_time)
                self.append_ip_history(ip_str, "get")
                self.ip_dict[ip_str]['get_time'] = time_now
                if not to_recheck:
                    self.ip_dict[ip_str]['links'] += 1
                self.ip_pointer += 1
                return ip_str
        except Exception as e:
            self.logger.exception("get_ip fail:%r", e)
        finally:
            self.ip_lock.release()

    def add_ip(self, ip_str, handshake_time=100, domain=None, server='gws', fail_times=0, down_fail=0, scan_result=True):
        if not isinstance(ip_str, str):
            self.logger.error("add_ip input")
            return

        time_now = time.time()
        if scan_result:
            self.check_local_network.report_ok(ip_str)
            success_time = time_now
        else:
            success_time = 0

        ip_str = str(ip_str)

        handshake_time = int(handshake_time)

        self.ip_lock.acquire()
        try:
            if ip_str in self.ip_dict:
                self.ip_dict[ip_str]['success_time'] = success_time
                self.ip_dict[ip_str]['handshake_time'] = handshake_time
                self.ip_dict[ip_str]['fail_times'] = fail_times
                if self.ip_dict[ip_str]['fail_time'] > 0:
                    self.ip_dict[ip_str]['fail_time'] = 0
                    self._add_ip_num(ip_str, 1)
                self.append_ip_history(ip_str, handshake_time)
                return False

            self.iplist_need_save = True
            self._add_ip_num(ip_str, 1)

            self.ip_dict[ip_str] = {'handshake_time':handshake_time, "fail_times":fail_times,
                                    "transfered_data":0, 'data_active':0,
                                    'domain':domain, 'server':server,
                                    "history":[[time_now, handshake_time]], "fail_time":0,
                                    "success_time":success_time, "get_time":0, "links":0,
                                    "down_fail":down_fail, "down_fail_time":0}

            if 'gws' not in server:
                return

            self.ip_list.append(ip_str)
        except Exception as e:
            self.logger.exception("add_ip err:%s", e)
        finally:
            self.ip_lock.release()

        return True

    def update_ip(self, ip_str, handshake_time):
        if not isinstance(ip_str, str):
            self.logger.error("update_ip input error:%s", ip_str)
            return

        handshake_time = int(handshake_time)
        if handshake_time < 5: # that's impossible
            self.logger.warn("%s handshake:%d impossible", ip_str, 1000 * handshake_time)
            return

        time_now = time.time()
        self.check_local_network.report_ok(ip_str)

        self.ip_lock.acquire()
        try:
            if ip_str in self.ip_dict:


                # Case: some good ip, average handshake time is 300ms
                # some times ip package lost cause handshake time become 2000ms
                # this ip will not return back to good ip front until all become bad
                # There for, prevent handshake time increase too quickly.
                org_time = self.ip_dict[ip_str]['handshake_time']
                if handshake_time - org_time > 500:
                    self.ip_dict[ip_str]['handshake_time'] = org_time + 500
                else:
                    self.ip_dict[ip_str]['handshake_time'] = handshake_time

                self.ip_dict[ip_str]['success_time'] = time_now
                if self.ip_dict[ip_str]['fail_times'] > 0:
                    self._add_ip_num(ip_str, 1)
                self.ip_dict[ip_str]['fail_times'] = 0
                self.append_ip_history(ip_str, handshake_time)
                self.ip_dict[ip_str]["fail_time"] = 0

                self.iplist_need_save = True

            #self.logger.debug("update ip:%s not exist", ip)
        except Exception as e:
            self.logger.error("update_ip err:%s", e)
        finally:
            self.ip_lock.release()

        self.save()

    def report_connect_fail(self, ip_str, reason="", force_remove=False):
        self.ip_lock.acquire()
        try:
            time_now = time.time()
            if not ip_str in self.ip_dict:
                self.logger.debug("report_connect_fail %s not exist", ip_str)
                return

            if force_remove:
                if self.ip_dict[ip_str]['fail_times'] == 0:
                    self._add_ip_num(ip_str, -1)
                del self.ip_dict[ip_str]

                if ip_str in self.ip_list:
                    self.ip_list.remove(ip_str)

                self.logger.info("remove ip:%s left amount:%d target_num:%d", ip_str, len(self.ip_dict), len(self.ip_list))
                return

            if self.ip_dict[ip_str]['links'] > 0:
                self.ip_dict[ip_str]['links'] -= 1

            self.check_local_network.report_fail(ip_str)
            # ignore if system network is disconnected.
            if not self.check_local_network.is_ok(ip_str):
                self.logger.debug("report_connect_fail network fail")
                return

            fail_time = self.ip_dict[ip_str]["fail_time"]
            if time_now - fail_time < 1:
                self.logger.debug("fail time too near %s", ip_str)
                return

            if self.ip_dict[ip_str]['fail_times'] == 0:
                self._add_ip_num(ip_str, -1)
            self.ip_dict[ip_str]['fail_times'] += 1
            self.append_ip_history(ip_str, "fail")
            self.ip_dict[ip_str]["fail_time"] = time_now

            # self.to_check_ip_queue.put((ip, time_now + 10))
            self.logger.debug("report_connect_fail:%s", ip_str)

        except Exception as e:
            self.logger.exception("report_connect_fail err:%s", e)
        finally:
            self.iplist_need_save = True
            self.ip_lock.release()

        if not self.is_ip_enough():
            self.search_more_ip()

    def report_connect_closed(self, ip_str, reason=""):
        # if reason not in ["idle timeout"]:
            # self.logger.debug("%s close:%s", ip, reason)
        if reason != "down fail":
            return

        self.ip_lock.acquire()
        try:
            time_now = time.time()
            if not ip_str in self.ip_dict:
                return

            if self.ip_dict[ip_str]['down_fail'] == 0:
                self._add_ip_num(ip_str, -1)

            self.ip_dict[ip_str]['down_fail'] += 1
            self.append_ip_history(ip_str, reason)
            self.ip_dict[ip_str]["down_fail_time"] = time_now
            # self.logger.debug("ssl_closed %s", ip)
        except Exception as e:
            self.logger.error("ssl_closed %s err:%s", ip_str, e)
        finally:
            self.ip_lock.release()

    def ssl_closed(self, ip_str, reason=""):
        #self.logger.debug("%s ssl_closed:%s", ip, reason)
        self.ip_lock.acquire()
        try:
            if ip_str in self.ip_dict:
                if self.ip_dict[ip_str]['links']:
                    self.ip_dict[ip_str]['links'] -= 1
                    self.append_ip_history(ip_str, "C[%s]" % reason)
                    # self.logger.debug("ssl_closed %s", ip)
        except Exception as e:
            self.logger.error("ssl_closed %s err:%s", ip_str, e)
        finally:
            self.ip_lock.release()

    def check_ip_process(self):
        pass


    def stop(self):
        self.running = False