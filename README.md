# Division 2 Status Watch

A small web app and background monitor for The Division 2. It checks Ubisoft's status display on a configurable 3–20 minute interval and sends one recovery alert when a previously reported maintenance, problem, or outage changes to operational.

The monitor reads the status widget linked from [Ubisoft's The Division 2 status page](https://www.ubisoft.com/en-us/game/the-division/the-division-2/status). If the page cannot be fetched or does not expose a clear live status, the dashboard says Unknown and the monitor waits for the next check. It does not treat the four-item status legend as the current game status.

## Deploy in Portainer

1. Put this folder on the server, or add it to a Git repository that Portainer can deploy.
2. In Portainer, choose **Stacks** → **Add stack**. Select this folder or repository and use compose.yaml.
3. Add a strong WEB_PASSWORD environment variable in the stack settings. WEB_USERNAME defaults to admin; you can change it. The stack will not start without WEB_PASSWORD.
4. Deploy the stack. Open http://SERVER-IP:3274 (or the host port set with PORT) and sign in with those credentials.
5. Choose the interval and notification method, enter the delivery settings, and save.

The named division2-status-data volume stores settings and last status across restarts. The web page uses HTTP Basic Authentication. Put it behind HTTPS before exposing it to the public internet.

## Email notifications

Use your mail provider's SMTP server, port, and connection security setting. Common combinations are STARTTLS on port 587 or SSL/TLS on port 465. Enter the sender address accepted by that provider. An SMTP app password may be required instead of the account password.

## Phone messages

Phone alerts are sent as SMS through Twilio. Enter the Twilio account SID, Auth Token, Twilio sender number, and your phone number. Numbers should use international format, such as +15551234567. Twilio may require a verified recipient or an SMS-enabled number, depending on your account and country.

## Gotify push notifications

Choose **Gotify push**, enter your Gotify server URL, and paste an application token from an application created in Gotify. The app posts the recovery message to Gotify's message endpoint with the token in the X-Gotify-Key header. You can use a server URL with a reverse-proxy base path, or paste a URL ending in /message. Gotify priority ranges from 0 to 10; the default of 5 produces a sound in the Gotify Android app. Install and connect the Gotify app on your phone to receive push notifications.

See Gotify's [push message instructions](https://gotify.net/docs/pushmsg) and [priority guide](https://gotify.net/docs/priority) for creating application tokens and choosing notification behavior.

## Behavior

- The first successful check records a baseline and does not send an alert.
- A notification is sent only for a transition from Maintenance, Problems, or Outage to Operational.
- Temporary fetch failures and unrecognized page markup show as Unknown and do not count as status changes.
- Notification credentials are stored in the persistent Docker volume and are not returned to the browser after saving.
- The monitor reports what Ubisoft's page publishes; it cannot verify connectivity from a particular player's device or platform.
