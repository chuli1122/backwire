Set shell = CreateObject("WScript.Shell")
' Set BACKWIRE_URL, BACKWIRE_TOKEN, BACKWIRE_NAME, and BACKWIRE_WORKDIR in the user environment.
' Store the token with user-only permissions; it is intentionally not passed on the command line.
shell.Run "python ""C:\path\to\backwire_client.py""", 0, False
