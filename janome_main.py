# janome_main.py
# Single entry point of the packaged program (JanomeJP4.exe, see installer/). One exe, three modes:
#   JanomeJP4.exe                 the web panel (port 8085)
#   JanomeJP4.exe --reader ...    the reader process (the panel starts it itself, with the same arguments as read_janome_jp4.py)
#   JanomeJP4.exe --diagnostic    the COM test page (port 8086)
# The reader must not run under eventlet's monkey patching, so each mode imports only what it needs.
# English comments only

import sys


def run():
    args = sys.argv[1:]
    if "--reader" in args:
        args.remove("--reader")
        import read_janome_jp4
        read_janome_jp4.main(args)
    elif "--diagnostic" in args:
        import serial_test_webapp
        serial_test_webapp.app.run(host="0.0.0.0", port=8086, debug=False)
    else:
        import app
        app.main()


if __name__ == "__main__":
    run()
