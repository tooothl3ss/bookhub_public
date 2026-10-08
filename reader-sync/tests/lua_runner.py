"""Run the real Lua source on a host. Prefer LuaJIT; liblua is a fallback.

Only for tests: no ARM emulator or changes to the shipped LuaJIT needed.
"""
import ctypes
import ctypes.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


def quote(s):
    equals = "="
    while "]" + equals + "]" in s:
        equals += "="
    return "[" + equals + "[" + s + "]" + equals + "]"


def main():
    executable = os.environ.get("LUA") or shutil.which("luajit") or shutil.which("lua")
    if executable:
        return subprocess.call([executable, *sys.argv[1:]])
    if sys.argv[1] == "-e":
        source = sys.argv[2]
    else:
        script = str(Path(sys.argv[1]).resolve())
        source = "arg={[0]=" + quote(script) + ""
        for i, value in enumerate(sys.argv[2:], 1):
            source += f",[{i}]=" + quote(value)
        source += "}; dofile(" + quote(script) + ")"
    if os.environ.get("KINDLE_TEST_LUAJIT_LUPA") == "1":
        from lupa.luajit21 import LuaRuntime
        LuaRuntime().execute(source)
        return 0
    name = ctypes.util.find_library("lua5.3") or ctypes.util.find_library("lua5.4")
    if not name:
        print("Install LuaJIT/Lua or set LUA to its executable", file=sys.stderr)
        return 1
    lib = ctypes.CDLL(name)
    lib.luaL_newstate.restype = ctypes.c_void_p
    lib.luaL_openlibs.argtypes = [ctypes.c_void_p]
    lib.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.lua_pcallk.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_void_p]
    lib.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lib.lua_tolstring.restype = ctypes.c_char_p
    lib.lua_close.argtypes = [ctypes.c_void_p]
    state = lib.luaL_newstate()
    lib.luaL_openlibs(state)
    status = lib.luaL_loadstring(state, source.encode())
    if not status:
        status = lib.lua_pcallk(state, 0, 0, 0, 0, None)
    if status:
        print(lib.lua_tolstring(state, -1, None).decode(), file=sys.stderr)
    lib.lua_close(state)
    return bool(status)


if __name__ == "__main__":
    raise SystemExit(main())
