"""TEMPORARY demo page for showing the AI service before the platform's mobile and web apps call it.

It is a stand-in for those apps, nothing more: it talks to the AI service only through the same /ai API the
platform will use (with the service key kept on the server), so what the demo shows is what the apps will get.
No other module imports anything from here.

Turned on only when DEMO_ENABLED=true (.env), and then open only to the computer it runs on (no sign-in);
otherwise every /demo address answers 404. Showing it to another device needs a sign-in added back first.

To remove it when the apps are connected:
  1. delete this folder (app/demo/) and tests/test_demo.py;
  2. delete the block marked "DEMO" at the end of app/api/main.py;
  3. remove DEMO_ENABLED from .env.
"""
