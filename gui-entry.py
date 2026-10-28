import sys

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--cli':
        from frlo_release.__main__ import main
        raise SystemExit(main(sys.argv[2:]))
    else:
        if sys.platform == 'win32' and getattr(sys, 'frozen', False):
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
        from frlo_release.gui import main
        main()
