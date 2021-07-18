#!/usr/bin/env python3
#
# Cross Platform and Multi Architecture Advanced Binary Emulation Framework
#

import os, pefile, pickle, secrets, traceback

from qiling.os.const import POINTER
from qiling.os.windows.fncc import CDECL
from qiling.os.windows.utils import *
from qiling.os.windows.structs import *
from qiling.exception import *
from qiling.const import *
from qiling.arch.x86_const import *
from .loader import QlLoader, Image
from qiling.os.memory import QlMemoryHeap


class QlPeCacheEntry:
    def __init__(self, data, cmdlines, import_symbols, import_table, entry_import_list):
        self.data = data
        self.cmdlines = cmdlines
        self.import_symbols = import_symbols
        self.import_table = import_table
        self.entry_import_list = entry_import_list

# A default simple cache implementation
class QlPeCache:
    def create_filename(self, path, address):
        return path + ".%x.cache2" % address

    def restore(self, path, address):
        fcache = self.create_filename(path, address)
        # pickle file cannot be outdated
        if os.path.exists(fcache) and os.stat(fcache).st_mtime > os.stat(path).st_mtime:
            with open(fcache, "rb") as fcache_file:
                return QlPeCacheEntry(*pickle.load(fcache_file))
        return None

    def save(self, path, address, entry):
        fcache = self.create_filename(path, address)
        data = (entry.data, entry.cmdlines, entry.import_symbols, entry.import_table, entry.entry_import_list)
        # cache this dll file
        with open(fcache, "wb") as fcache_file:
            pickle.dump(data, fcache_file)


class Process():
    def __init__(self, ql):
        self.ql = ql

    def align(self, size, unit):
        return (size // unit + (1 if size % unit else 0)) * unit

    def load_dll(self, dll_name, driver=False):
        dll_name = dll_name.decode()

        self.ql.dlls = os.path.join("Windows", "System32")

        if 'C:\\' in dll_name.upper():
            path = self.ql.os.path.transform_to_real_path(dll_name)
            dll_name = path_leaf(dll_name)
        else:
            dll_name = dll_name.lower()
            if not is_file_library(dll_name):
                dll_name = dll_name + ".dll"
            path = os.path.join(self.ql.rootfs, self.ql.dlls, dll_name)

        if not os.path.exists(path):
            raise QlErrorFileNotFound("Cannot find dll in %s" % path)

        # If the dll is already loaded
        if dll_name in self.dlls:
            return self.dlls[dll_name]
        else:
            self.dlls[dll_name] = self.dll_last_address

        self.ql.log.info("Loading %s to 0x%x" % (path, self.dll_last_address))

        if self.libcache:
            cached = self.libcache.restore(path, self.dll_last_address)
        else:
            cached = None

        if cached:
            data = cached.data
            import_symbols = cached.import_symbols
            import_table = cached.import_table
            entry_import_list = cached.entry_import_list
            for entry in cached.cmdlines:
                self.set_cmdline(entry['name'], entry['address'], data)
            self.ql.log.info("Loaded %s from cache" % path)
        else:
            dll = pefile.PE(path, fast_load=True)
            dll.parse_data_directories()
            warnings = dll.get_warnings()
            if warnings:
                self.ql.log.warning(f'Warnings while loading {path}:')
                for warning in warnings:
                    self.ql.log.warning(f' - {warning}')

            # [Room for Improvement] too much time when kernelbase.dll is loaded. 
            #self.ql.log.debug('relocate {}, {:x}'.format(dll_name, self.dll_last_address))
            dll.relocate_image(self.dll_last_address)

            # make entry import table for resolving dll address
            entry_import_list = {}
            if hasattr(dll, 'DIRECTORY_ENTRY_IMPORT'):
                for entry_import in dll.DIRECTORY_ENTRY_IMPORT:
                    for entry_import_symbol in entry_import.imports:
                        if entry_import_symbol.name == None:
                            continue
                        entry_import_list[entry_import_symbol.address] = {
                            'symbol': entry_import_symbol.name.decode('utf-8'), 
                            'dll': entry_import.dll.decode('utf-8').lower()
                        }
                        
            
            data = bytearray(dll.get_memory_mapped_image())
            cmdlines = []

            import_symbols = {}
            import_table = {}
            for entry in dll.DIRECTORY_ENTRY_EXPORT.symbols:
                import_symbols[self.dll_last_address + entry.address] = {"name": entry.name,
                                                                              "ordinal": entry.ordinal,
                                                                              "dll": dll_name.split('.')[0]
                                                                              }
                if entry.name:
                    import_table[entry.name] = self.dll_last_address + entry.address
                import_table[entry.ordinal] = self.dll_last_address + entry.address
                cmdline_entry = self.set_cmdline(entry.name, entry.address, data)
                if cmdline_entry:
                    cmdlines.append(cmdline_entry)

            if self.libcache:
                cached = QlPeCacheEntry(data, cmdlines, import_symbols, import_table, entry_import_list)
                self.libcache.save(path, self.dll_last_address, cached)
                self.ql.log.info("Cached %s" % path)

        # Add dll to IAT
        try:
            self.import_address_table[dll_name] = import_table
        except Exception as ex:
            self.ql.log.exception(f'Unable to add {dll_name} to IAT')

        try:
            self.entry_import_table[dll_name] = entry_import_list
        except Exception as ex:
            self.ql.log.exception(f'Unable to add {dll_name} to entry_import_table')

        try:
            self.import_symbols.update(import_symbols)
        except Exception as ex:
            self.ql.log.exception(f'Unable to add {dll_name} import symbols')

        dll_base = self.dll_last_address
        dll_len = self.ql.mem.align(len(bytes(data)), 0x1000)
        self.dll_size += dll_len
        self.ql.mem.map(dll_base, dll_len, info=dll_name)
        self.ql.mem.write(dll_base, bytes(data))
        self.dll_last_address += dll_len

        # if this is NOT a driver, add dll to ldr data
        if not driver:
            self.add_ldr_data_table_entry(dll_name)

        # add DLL to coverage images
        self.images.append(Image(dll_base, dll_base+dll_len, path))

        self.ql.log.info("Done with loading %s" % path)

        return dll_base


    def _alloc_cmdline(self, wide):
        addr = self.ql.os.heap.alloc(len(self.cmdline) * (2 if wide else 1))
        packed_addr = self.ql.pack(addr)
        return addr, packed_addr

    def set_cmdline(self, name, address, memory):
        cmdline_entry = None
        if name == b"_acmdln":
            addr, packed_addr = self._alloc_cmdline(wide=False)
            cmdline_entry = {"name": name, "address": address}
            memory[address:address + self.ql.pointersize] = packed_addr
            self.ql.mem.write(addr, self.cmdline)
        elif name == b"_wcmdln":
            addr, packed_addr = self._alloc_cmdline(wide=True)
            cmdline_entry = {"name": name, "address": address}
            memory[address:address + self.ql.pointersize] = packed_addr
            encoded = self.cmdline.decode('ascii').encode('UTF-16LE')
            self.ql.mem.write(addr, encoded)

        return cmdline_entry

    def init_tib(self):
        if self.ql.archtype == QL_ARCH.X86:
            teb_addr = self.structure_last_addr
        else:
            gs = self.structure_last_addr
            self.structure_last_addr += 0x30
            teb_addr = self.structure_last_addr

        self.ql.log.info("TEB addr is 0x%x" %teb_addr)

        teb_size = len(TEB(self.ql).bytes())
        teb_data = TEB(
            self.ql,
            base=teb_addr,
            peb_address=teb_addr + teb_size,
            stack_base=self.stack_address + self.stack_size,
            stack_limit=self.stack_size,
            Self=teb_addr)

        self.ql.mem.write(teb_addr, teb_data.bytes())

        self.structure_last_addr += teb_size
        if self.ql.archtype == QL_ARCH.X8664:
            # TEB
            self.ql.mem.write(gs + 0x30, self.ql.pack64(teb_addr))
            # PEB
            self.ql.mem.write(gs + 0x60, self.ql.pack64(teb_addr + teb_size))

        self.TEB = self.ql.TEB = teb_data

    def init_peb(self):
        peb_addr = self.structure_last_addr

        self.ql.log.info("PEB addr is 0x%x" % peb_addr)

        # we must set an heap, will try to retrieve this value. Is ok to be all \x00
        process_heap = self.ql.os.heap.alloc(0x100)
        peb_data = PEB(self.ql, base=peb_addr, process_heap=process_heap,
                       number_processors=self.ql.os.profile.getint("HARDWARE",
                                                                   "number_processors"))
        peb_data.LdrAddress = peb_addr + peb_data.size
        peb_data.write(peb_addr)
        self.structure_last_addr += peb_data.size
        self.PEB = self.ql.PEB = peb_data

    def init_ldr_data(self):
        ldr_addr = self.structure_last_addr
        ldr_size = len(LdrData(self.ql).bytes())
        ldr_data = LdrData(
            self.ql,
            base=ldr_addr,
            in_load_order_module_list={
                'Flink': ldr_addr + 2 * self.ql.pointersize,
                'Blink': ldr_addr + 2 * self.ql.pointersize
            },
            in_memory_order_module_list={
                'Flink': ldr_addr + 4 * self.ql.pointersize,
                'Blink': ldr_addr + 4 * self.ql.pointersize
            },
            in_initialization_order_module_list={
                'Flink': ldr_addr + 6 * self.ql.pointersize,
                'Blink': ldr_addr + 6 * self.ql.pointersize
            }
        )
        self.ql.mem.write(ldr_addr, ldr_data.bytes())
        self.structure_last_addr += ldr_size
        self.LDR = self.ql.LDR = ldr_data

    def add_ldr_data_table_entry(self, dll_name):
        dll_base = self.dlls[dll_name]
        path = "C:\\Windows\\System32\\" + dll_name
        ldr_table_entry_size = len(LdrDataTableEntry(self.ql).bytes())
        base = self.ql.os.heap.alloc(ldr_table_entry_size)
        ldr_table_entry = LdrDataTableEntry(self.ql,
                                            base=base,
                                            in_load_order_links={'Flink': 0, 'Blink': 0},
                                            in_memory_order_links={'Flink': 0, 'Blink': 0},
                                            in_initialization_order_links={'Flink': 0, 'Blink': 0},
                                            dll_base=dll_base,
                                            entry_point=0,
                                            full_dll_name=path,
                                            base_dll_name=dll_name)

        # Flink
        if len(self.ldr_list) == 0:
            flink = self.LDR
            ldr_table_entry.InLoadOrderLinks['Flink'] = flink.InLoadOrderModuleList['Flink']
            ldr_table_entry.InMemoryOrderLinks['Flink'] = flink.InMemoryOrderModuleList['Flink']
            ldr_table_entry.InInitializationOrderLinks['Flink'] = flink.InInitializationOrderModuleList['Flink']

            flink.InLoadOrderModuleList['Flink'] = ldr_table_entry.base
            flink.InMemoryOrderModuleList['Flink'] = ldr_table_entry.base + 2 * self.ql.pointersize
            flink.InInitializationOrderModuleList['Flink'] = ldr_table_entry.base + 4 * self.ql.pointersize

        else:
            flink = self.ldr_list[-1]
            ldr_table_entry.InLoadOrderLinks['Flink'] = flink.InLoadOrderLinks['Flink']
            ldr_table_entry.InMemoryOrderLinks['Flink'] = flink.InMemoryOrderLinks['Flink']
            ldr_table_entry.InInitializationOrderLinks['Flink'] = flink.InInitializationOrderLinks['Flink']

            flink.InLoadOrderLinks['Flink'] = ldr_table_entry.base
            flink.InMemoryOrderLinks['Flink'] = ldr_table_entry.base + 2 * self.ql.pointersize
            flink.InInitializationOrderLinks['Flink'] = ldr_table_entry.base + 4 * self.ql.pointersize

        # Blink
        blink = self.LDR
        ldr_table_entry.InLoadOrderLinks['Blink'] = blink.InLoadOrderModuleList['Blink']
        ldr_table_entry.InMemoryOrderLinks['Blink'] = blink.InMemoryOrderModuleList['Blink']
        ldr_table_entry.InInitializationOrderLinks['Blink'] = blink.InInitializationOrderModuleList['Blink']

        blink.InLoadOrderModuleList['Blink'] = ldr_table_entry.base
        blink.InMemoryOrderModuleList['Blink'] = ldr_table_entry.base + 2 * self.ql.pointersize
        blink.InInitializationOrderModuleList['Blink'] = ldr_table_entry.base + 4 * self.ql.pointersize

        self.ql.mem.write(flink.base, flink.bytes())
        self.ql.mem.write(blink.base, blink.bytes())
        self.ql.mem.write(ldr_table_entry.base, ldr_table_entry.bytes())

        self.ldr_list.append(ldr_table_entry)

    def init_exports(self):
        if self.ql.code:
            return
        if self.pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']].VirtualAddress != 0:
            # Do a full load if IMAGE_DIRECTORY_ENTRY_EXPORT is present so we can load the exports
            self.pe.full_load()
        else:
            return

        try:
            # parse directory entry export
            dll_name = os.path.basename(self.path)
            self.import_address_table[dll_name] = {} 
            for entry in self.pe.DIRECTORY_ENTRY_EXPORT.symbols:
                self.export_symbols[self.pe_image_address + entry.address] = {'name': entry.name, 'ordinal': entry.ordinal}
                self.import_address_table[dll_name][entry.name] = self.pe_image_address + entry.address
                self.import_address_table[dll_name][entry.ordinal] = self.pe_image_address + entry.address
        except:
            self.ql.log.info('Failed to load exports for %s:\n%s' % (self.ql.argv, traceback.format_exc()))

    def init_driver_object(self):
        # PDRIVER_OBJECT DriverObject
        driver_object_addr = self.structure_last_addr
        self.ql.log.info("Driver object addr is 0x%x" %driver_object_addr)

        if self.ql.archtype == QL_ARCH.X86:
            self.driver_object = DRIVER_OBJECT32(self.ql, driver_object_addr)
        elif self.ql.archtype == QL_ARCH.X8664:
            self.driver_object = DRIVER_OBJECT64(self.ql, driver_object_addr)

        driver_object_size = ctypes.sizeof(self.driver_object)
        self.ql.mem.write(driver_object_addr, bytes(self.driver_object))
        self.structure_last_addr += driver_object_size
        self.ql.driver_object_address = driver_object_addr


    def init_registry_path(self):
        # PUNICODE_STRING RegistryPath
        regitry_path_addr = self.structure_last_addr
        self.ql.log.info("Registry path addr is 0x%x" %regitry_path_addr)

        if self.ql.archtype == QL_ARCH.X86:
            regitry_path_data = UNICODE_STRING32(0, 0, regitry_path_addr)
        elif self.ql.archtype == QL_ARCH.X8664:
            regitry_path_data = UNICODE_STRING64(0, 0, regitry_path_addr)

        regitry_path_size = ctypes.sizeof(regitry_path_data)
        self.ql.mem.write(regitry_path_addr, bytes(regitry_path_data))
        self.structure_last_addr += regitry_path_size
        self.ql.regitry_path_address = regitry_path_addr


    def init_eprocess(self):
        addr = self.structure_last_addr
        self.ql.log.info("EPROCESS is is 0x%x" %addr)


        if self.ql.archtype == QL_ARCH.X86:
            self.eprocess_object = EPROCESS32(self.ql, addr)
        elif self.ql.archtype == QL_ARCH.X8664:
            self.eprocess_object = EPROCESS64(self.ql, addr)            

        size = ctypes.sizeof(self.eprocess_object)
        self.ql.mem.write(addr, bytes(self.driver_object))
        self.structure_last_addr += size
        self.ql.eprocess_address = addr


    def init_ki_user_shared_data(self):
        '''
        https://www.geoffchappell.com/studies/windows/km/ntoskrnl/structs/kuser_shared_data/index.htm

		struct information:
		https://doxygen.reactos.org/d8/dae/modules_2rostests_2winetests_2ntdll_2time_8c_source.html
        '''
        if self.ql.archtype == QL_ARCH.X86:
            KI_USER_SHARED_DATA = 0xFFDF0000
        elif self.ql.archtype == QL_ARCH.X8664:
            KI_USER_SHARED_DATA = 0xFFFFF78000000000

        self.ql.log.info("KI_USER_SHARED_DATA is 0x%x" %KI_USER_SHARED_DATA)

        shared_user_data = KUSER_SHARED_DATA()

        shared_user_data_len = self.align(ctypes.sizeof(KUSER_SHARED_DATA), 0x1000)
        self.ql.uc.mem_map(KI_USER_SHARED_DATA, shared_user_data_len)
        self.ql.mem.write(KI_USER_SHARED_DATA, bytes(shared_user_data))
    

class QlLoaderPE(QlLoader, Process):
    def __init__(self, ql):
        super(QlLoaderPE, self).__init__(ql)
        self.ql         = ql
        if type(self.ql.libcache) == bool:
            self.libcache = QlPeCache() if self.ql.libcache else None
        else:
            self.libcache = self.ql.libcache
        self.path       = self.ql.path
        self.is_driver  = False

    def run(self):
        self.init_dlls = [b"ntdll.dll", b"kernel32.dll", b"user32.dll"]
        self.sys_dlls = [b"ntdll.dll", b"kernel32.dll"]
        self.pe_entry_point = 0
        self.sizeOfStackReserve = 0        

        if not self.ql.code:
            self.pe = pefile.PE(self.path, fast_load=True)
            self.is_driver = self.pe.is_driver()
            if self.is_driver == True:
                self.init_dlls.append(b"ntoskrnl.exe")
                self.sys_dlls.append(b"ntoskrnl.exe")
            
        if self.ql.archtype == QL_ARCH.X86:
            self.stack_address = int(self.ql.os.profile.get("OS32", "stack_address"), 16)
            self.stack_size = int(self.ql.os.profile.get("OS32", "stack_size"), 16)
            self.image_address = int(self.ql.os.profile.get("OS32", "image_address"), 16)
            self.dll_address = int(self.ql.os.profile.get("OS32", "dll_address"), 16)
            self.entry_point = int(self.ql.os.profile.get("OS32", "entry_point"), 16)
            self.ql.os.heap_base_address = int(self.ql.os.profile.get("OS32", "heap_address"), 16)
            self.ql.os.heap_base_size = int(self.ql.os.profile.get("OS32", "heap_size"), 16)
            self.structure_last_addr = FS_SEGMENT_ADDR
        elif self.ql.archtype == QL_ARCH.X8664:
            self.stack_address = int(self.ql.os.profile.get("OS64", "stack_address"), 16)
            self.stack_size = int(self.ql.os.profile.get("OS64", "stack_size"), 16)
            self.image_address = int(self.ql.os.profile.get("OS64", "image_address"), 16)
            self.dll_address = int(self.ql.os.profile.get("OS64", "dll_address"), 16)
            self.entry_point = int(self.ql.os.profile.get("OS64", "entry_point"), 16)
            self.ql.os.heap_base_address = int(self.ql.os.profile.get("OS64", "heap_address"), 16)
            self.ql.os.heap_base_size = int(self.ql.os.profile.get("OS64", "heap_size"), 16)
            self.structure_last_addr = GS_SEGMENT_ADDR

        self.dlls = {}
        self.import_symbols = {}
        self.export_symbols = {}
        self.import_address_table = {}
        self.entry_import_table = {}
        self.ldr_list = []
        self.pe_image_address = 0
        self.pe_image_address_size = 0
        self.dll_size = 0
        self.dll_last_address = self.dll_address
        # compatible with ql.__enable_bin_patch()
        self.load_address = 0
        self.ql.os.heap = QlMemoryHeap(self.ql, self.ql.os.heap_base_address, self.ql.os.heap_base_address + self.ql.os.heap_base_size)
        self.ql.os.setupComponents()
        self.ql.os.entry_point = self.entry_point
        cmdline = (str(self.ql.os.userprofile)) + "Desktop\\" + self.ql.targetname
        self.filepath = bytes(cmdline + "\x00", "utf-8")
        for arg in self.argv[1:]:
            if ' ' in arg:
                cmdline += f' "{arg}"'
            else:
                cmdline += f' {arg}'
        cmdline += "\x00"
        self.cmdline = bytes(cmdline, "utf-8")

        self.load()

    def init_thread_information_block(self):
        super().init_tib()
        super().init_peb()
        super().init_ldr_data()
        super().init_exports()

    def load(self):
        # set stack pointer
        self.ql.log.info("Initiate stack address at 0x%x " % self.stack_address)
        self.ql.mem.map(self.stack_address, self.stack_size, info="[stack]")

        if self.path and not self.ql.code:
            # for simplicity, no image base relocation
            self.pe_image_address = self.pe.OPTIONAL_HEADER.ImageBase
            self.pe_image_address_size = self.ql.mem.align(self.pe.OPTIONAL_HEADER.SizeOfImage, 0x1000)

            if self.pe_image_address + self.pe_image_address_size > self.ql.os.heap_base_address:
                # pe reloc
                self.pe_image_address = self.image_address
                self.pe.relocate_image(self.image_address)

            self.entry_point = self.pe_entry_point = self.pe_image_address + self.pe.OPTIONAL_HEADER.AddressOfEntryPoint
            self.sizeOfStackReserve = self.pe.OPTIONAL_HEADER.SizeOfStackReserve
            self.ql.log.info("Loading %s to 0x%x" % (self.path, self.pe_image_address))
            self.ql.log.info("PE entry point at 0x%x" % self.entry_point)
            self.images.append(Image(self.pe_image_address, self.pe_image_address + self.pe.NT_HEADERS.OPTIONAL_HEADER.SizeOfImage, self.path))

            # make entry import table for resolving dll address
            entry_import_list = {}
            if hasattr(self.pe, 'DIRECTORY_ENTRY_IMPORT'):
                for entry_import in self.pe.DIRECTORY_ENTRY_IMPORT:
                    for entry_import_symbol in entry_import.imports:
                        #print(entry_import_symbol.address, self.pe_image_address, self.image_address)
                        entry_import_list[entry_import_symbol.address] = {
                            'symbol': entry_import_symbol.name.decode('utf-8'), 
                            'dll': entry_import.dll.decode('utf-8').lower()
                        }
                self.entry_import_table['[PE]'] = entry_import_list
                        

            # Stack should not init at the very bottom. Will cause errors with Dlls
            sp = self.stack_address + self.stack_size - 0x1000

            if self.ql.archtype == QL_ARCH.X86:
                self.ql.reg.esp = sp
                self.ql.reg.ebp = sp

                if self.pe.is_dll():
                    self.ql.log.debug('Setting up DllMain args')
                    load_addr_bytes = self.pe_image_address.to_bytes(length=4, byteorder='little')

                    self.ql.log.debug('Writing 0x%08X (IMAGE_BASE) to [ESP+4](0x%08X)' % (self.pe_image_address, sp + 0x4))
                    self.ql.mem.write(sp + 0x4, load_addr_bytes)

                    self.ql.log.debug('Writing 0x01 (DLL_PROCESS_ATTACH) to [ESP+8](0x%08X)' % (sp + 0x8))
                    self.ql.mem.write(sp + 0x8, int(1).to_bytes(length=4, byteorder='little'))

            elif self.ql.archtype == QL_ARCH.X8664:
                self.ql.reg.rsp = sp
                self.ql.reg.rbp = sp

                if self.pe.is_dll():
                    self.ql.log.debug('Setting up DllMain args')

                    self.ql.log.debug('Setting RCX (arg1) to %16X (IMAGE_BASE)' % (self.pe_image_address))
                    self.ql.reg.rcx = self.pe_image_address

                    self.ql.log.debug('Setting RDX (arg2) to 1 (DLL_PROCESS_ATTACH)')
                    self.ql.reg.rdx = 1
            else:
                raise QlErrorArch("Unknown ql.arch")

            # if this is NOT a driver, init tib/peb/ldr
            if not self.is_driver:  # userland program
                self.init_thread_information_block()
            else:   # Windows kernel driver
                super().init_driver_object()
                super().init_registry_path()
                super().init_eprocess()
                super().init_ki_user_shared_data()

                # setup IRQ Level in CR8 to PASSIVE_LEVEL (0)
                self.ql.uc.reg_write(UC_X86_REG_CR8, 0)

                # setup CR4, some drivers may check this at initialized time
                self.ql.uc.reg_write(UC_X86_REG_CR4, 0x6f8)

                self.ql.log.debug('Setting up DriverEntry args')
                self.ql.stop_execution_pattern = 0xDEADC0DE

                if self.ql.archtype == QL_ARCH.X86:  # Win32
                    if not self.ql.stop_options.any:
                        # We know that a driver will return,
                        # so if the user did not configure stop options, write a sentinel return value
                        self.ql.mem.write(sp, self.ql.stop_execution_pattern.to_bytes(length=4, byteorder='little'))

                    self.ql.log.debug('Writing 0x%08X (PDRIVER_OBJECT) to [ESP+4](0x%08X)' % (self.ql.driver_object_address, sp+0x4))
                    self.ql.log.debug('Writing 0x%08X (RegistryPath) to [ESP+8](0x%08X)' % (self.ql.regitry_path_address, sp+0x8))
                elif self.ql.archtype == QL_ARCH.X8664:  # Win64
                    if not self.ql.stop_options.any:
                        # We know that a driver will return,
                        # so if the user did not configure stop options, write a sentinel return value
                        self.ql.mem.write(sp, self.ql.stop_execution_pattern.to_bytes(length=8, byteorder='little'))

                    self.ql.log.debug('Setting RCX (arg1) to %16X (PDRIVER_OBJECT)' % (self.ql.driver_object_address))
                    self.ql.log.debug('Setting RDX (arg2) to %16X (PUNICODE_STRING)' % (self.ql.regitry_path_address))

                # setup args for DriverEntry()
                self.ql.os.fcall = self.ql.os.fcall_select(CDECL)
                self.ql.os.fcall.writeParams(((POINTER, self.ql.driver_object_address), (POINTER, self.ql.regitry_path_address)))

            # mmap PE file into memory
            self.ql.mem.map(self.pe_image_address, self.align(self.pe_image_address_size, 0x1000), info="[PE]")
            self.pe.parse_data_directories()
            data = bytearray(self.pe.get_memory_mapped_image())
            self.ql.mem.write(self.pe_image_address, bytes(data))
            # setup IMAGE_LOAD_CONFIG_DIRECTORY
            if self.pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG']].VirtualAddress != 0:
                SecurityCookie_rva = self.pe.DIRECTORY_ENTRY_LOAD_CONFIG.struct.SecurityCookie - self.pe.OPTIONAL_HEADER.ImageBase
                SecurityCookie_value = default_security_cookie_value = self.ql.mem.read(self.pe_image_address+SecurityCookie_rva, self.ql.pointersize)
                while SecurityCookie_value == default_security_cookie_value:
                    SecurityCookie_value = secrets.token_bytes(self.ql.pointersize)
                    # rol     rcx, 10h (rcx: cookie)
                    # test    cx, 0FFFFh
                    SecurityCookie_value_array = bytearray(SecurityCookie_value)
                    # Sanity question: We are always little endian, right?
                    SecurityCookie_value_array[-2:] = b'\x00\x00'
                    SecurityCookie_value = bytes(SecurityCookie_value_array)
                self.ql.mem.write(self.pe_image_address+SecurityCookie_rva, SecurityCookie_value)

            # Add main PE to ldr_data_table
            mod_name = os.path.basename(self.path)
            self.dlls[mod_name] = self.pe_image_address
            # only userland code need LDR table
            if not self.is_driver:
                super().add_ldr_data_table_entry(mod_name)

            # load system dlls
            sys_dlls = self.sys_dlls
            for each in sys_dlls:
                super().load_dll(each, self.is_driver)

            self.ql.log.debug("Done with loading %s" % self.path)
            self.ql.os.entry_point = self.entry_point
            self.ql.os.pid = 101

        elif self.ql.code:
            self.filepath = b""
            if self.ql.archtype == QL_ARCH.X86:
                self.ql.reg.esp = self.stack_address + 0x3000
                self.ql.reg.ebp = self.ql.reg.esp
            elif self.ql.archtype == QL_ARCH.X8664:
                self.ql.reg.rsp = self.stack_address + 0x3000
                self.ql.reg.rbp = self.ql.reg.rsp

            # load shellcode in
            self.ql.mem.map(self.entry_point, self.ql.os.code_ram_size, info="[shellcode_base]")
            # rewrite entrypoint for windows shellcode
            self.ql.os.entry_point = self.entry_point
            self.ql.os.pid = 101

            self.ql.mem.write(self.entry_point, self.ql.code)
            
            self.init_thread_information_block()
            # load dlls
            for each in self.init_dlls:
                super().load_dll(each)

        # move entry_point to ql.os
        self.ql.os.entry_point = self.entry_point
        self.init_sp = self.ql.reg.arch_sp

    def _get_export_symbol_from_api_dll(self, api_dll_name, target_symbol):
        """
        API set dll (https://docs.microsoft.com/en-us/windows/win32/apiindex/windows-apisets) loader.
        The function extract actual export symbol and DLL name from API set dll(likes 'api-ms-xxxx.dll').

        Args:
            api_dll_name (str): API Set DLL name
            target_symbol (str): The symbol looking for.
        Return:
            dll_name (str): actual DLL name.
            export_symbol (str): Actual symbol name.
        """

        def _get_string_from_pe(api_dll, target_symbol):
            offset = 0
            string = ''
            dll_base = api_dll.OPTIONAL_HEADER.ImageBase

            export_symbol_list = list(filter(lambda x: x.name == target_symbol.encode('utf-8'), api_dll.DIRECTORY_ENTRY_EXPORT.symbols))
            if len(export_symbol_list) == 0:
                self.ql.log.debug("Error: can't find symbol from API Set dll (symbol: %s, apiset dll: %s" % (api_dll, target_symbol))
                return ''

            while True:
                char = api_dll.get_data(export_symbol_list[0].address+offset, 1)
                if char == b'\x00':
                    break

                string += char.decode('utf-8')
                offset += 1

            return string
        
        try:
            api_dll = pefile.PE(os.path.join(self.ql.rootfs, self.ql.dlls, api_dll_name))
        except:
            self.ql.log.warning('Failed to load API dll %s' % (api_dll_name))
            return None, None

        # result of _get_string_from_pe has 2 types, "kernel32.GetPriorityClass" and "advapi32.dll.OpenProcessToken"
        # therefore, both types need to be supported.
        dll_name, export_symbol = _get_string_from_pe(api_dll, target_symbol).rsplit('.', 1)
        if dll_name[-4:] == '.dll':
            return dll_name, export_symbol
        return dll_name+'.dll', export_symbol