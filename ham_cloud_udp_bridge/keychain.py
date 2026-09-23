"""macOS generic-password storage using Security.framework, without shell tools.

A single JSON credential bundle is one Keychain item, so all credentials are
updated atomically. No secret is passed in process arguments or written to disk.
This unsigned console app uses the user's default file-based Keychain. Access is
controlled by macOS for the Python executable; this is not a sandbox entitlement.
"""
import ctypes as C
from contextlib import contextmanager
import json
import sys


class KeychainError(ValueError):
    pass


class KeychainStore:
    SERVICE = 'HAM Cloud UDP Bridge'

    def __init__(self, profile):
        if sys.platform != 'darwin':
            raise KeychainError('HAM Cloud UDP Bridge requires macOS Keychain. No plaintext fallback is available.')
        self.profile = profile
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
        signatures = {
            'CFStringCreateWithCString': (C.c_void_p, [C.c_void_p, C.c_char_p, C.c_uint32]),
            'CFDataCreate': (C.c_void_p, [C.c_void_p, C.c_void_p, C.c_long]),
            'CFDataGetLength': (C.c_long, [C.c_void_p]),
            'CFDataGetBytePtr': (C.c_void_p, [C.c_void_p]),
            'CFDictionaryCreate': (C.c_void_p, [C.c_void_p, C.c_void_p, C.c_void_p, C.c_long, C.c_void_p, C.c_void_p]),
            'CFRelease': (None, [C.c_void_p]),
        }
        for name, (result, args) in signatures.items():
            function = getattr(self.cf, name)
            function.restype, function.argtypes = result, args
        for name in ('SecItemCopyMatching', 'SecItemAdd', 'SecItemUpdate'):
            function = getattr(self.sec, name)
            function.restype, function.argtypes = C.c_int32, [C.c_void_p, C.c_void_p]
        self.sec.SecItemDelete.restype = C.c_int32
        self.sec.SecItemDelete.argtypes = [C.c_void_p]

    def symbol(self, name):
        library = self.cf if name.startswith('kCF') else self.sec
        return C.c_void_p.in_dll(library, name).value

    @contextmanager
    def dictionary(self, attributes):
        owned = []
        try:
            keys, values = [], []
            for key, value in attributes.items():
                keys.append(self.symbol(key))
                if isinstance(value, bytes):
                    pointer = self.cf.CFDataCreate(None, value, len(value))
                    owned.append(pointer)
                elif isinstance(value, tuple):
                    pointer = self.symbol(value[0])
                else:
                    pointer = self.cf.CFStringCreateWithCString(None, value.encode('utf-8'), 0x08000100)
                    owned.append(pointer)
                if not pointer:
                    raise KeychainError('Could not allocate Keychain request')
                values.append(pointer)
            key_array = (C.c_void_p * len(keys))(*keys)
            value_array = (C.c_void_p * len(values))(*values)
            key_callbacks = C.addressof(C.c_byte.in_dll(self.cf, 'kCFTypeDictionaryKeyCallBacks'))
            value_callbacks = C.addressof(C.c_byte.in_dll(self.cf, 'kCFTypeDictionaryValueCallBacks'))
            dictionary = self.cf.CFDictionaryCreate(None, key_array, value_array, len(keys), key_callbacks, value_callbacks)
            if not dictionary:
                raise KeychainError('Could not allocate Keychain request')
            owned.append(dictionary)
            yield dictionary
        finally:
            for pointer in reversed(owned):
                if pointer:
                    self.cf.CFRelease(pointer)

    def query(self):
        return {'kSecClass': ('kSecClassGenericPassword',),
                'kSecAttrService': self.SERVICE, 'kSecAttrAccount': self.profile}

    @staticmethod
    def check(status):
        if status != 0:
            raise KeychainError(f'macOS Keychain access failed (status {status}). Unlock your login Keychain and allow this Python app access, then try again. Credentials were not saved to a plaintext file.')

    def read(self):
        result = C.c_void_p()
        with self.dictionary({**self.query(), 'kSecReturnData': ('kCFBooleanTrue',),
                              'kSecMatchLimit': ('kSecMatchLimitOne',)}) as query:
            status = self.sec.SecItemCopyMatching(query, C.byref(result))
        if status == -25300:  # errSecItemNotFound
            return {}
        self.check(status)
        try:
            data = C.string_at(self.cf.CFDataGetBytePtr(result), self.cf.CFDataGetLength(result))
            payload = json.loads(data)
            if not isinstance(payload, dict) or any(not isinstance(v, str) for v in payload.values()):
                raise ValueError()
            return payload
        except (ValueError, UnicodeDecodeError):
            raise KeychainError('The stored Keychain credential bundle is invalid') from None
        finally:
            self.cf.CFRelease(result)

    def write(self, credentials):
        payload = {k: v for k, v in credentials.items() if v}
        if not payload:
            with self.dictionary(self.query()) as query:
                status = self.sec.SecItemDelete(query)
            if status != -25300:
                self.check(status)
            return
        data = json.dumps(payload).encode('utf-8')
        with self.dictionary(self.query()) as query, self.dictionary({'kSecValueData': data}) as changes:
            status = self.sec.SecItemUpdate(query, changes)
        if status == -25300:
            with self.dictionary({**self.query(), 'kSecValueData': data,
                                  'kSecAttrLabel': 'HAM Cloud UDP Bridge credentials'}) as item:
                status = self.sec.SecItemAdd(item, None)
        self.check(status)
