# The Division 2 Status Watch

A small web app and background monitor for The Division 2. It checks Ubisoft's status display every 3, 5, 10, 15, 20, 25, 30, or 60 minutes and can send a notification when the reported status changes to any status you select.

The monitor reads the status widget linked from [Ubisoft's The Division 2 status page](https://www.ubisoft.com/en-us/game/the-division/the-division-2/status). If the page is reachable but does not expose a clear live status, the dashboard shows Online as its default and the monitor waits for the next check. If the page cannot be fetched, the dashboard shows Unknown. It does not treat the four-item status legend as the current game status.

## Deploy in Portainer

1. Put this folder on the server, or add it to a Git repository that Portainer can deploy.
2. In Portainer, choose **Stacks** → **Add stack**. Select this folder or repository and use compose.yaml.
3. Add a strong WEB_PASSWORD environment variable in the stack settings. WEB_USERNAME defaults to admin; you can change it. The stack will not start without WEB_PASSWORD.
4. Deploy the stack. Open http://SERVER-IP:3274 (or the host port set with PORT) and sign in with those credentials.
5. Choose the interval, select which status changes should trigger alerts, choose a notification method, enter the delivery settings, and save. Online (Operational) is selected by default to preserve the original recovery alert behaviour.

The named division2-status-data volume stores settings and last status across restarts. The web page uses HTTP Basic Authentication. Put it behind HTTPS before exposing it to the public internet.

## Android companion app

The Android app (version 2.0) connects to this Web App; it does not connect to Portainer. Download the single-file `TheDivision2StatusWatch-2.0.apk` from the [GitHub Releases page](https://github.com/marxdrive/The-Division-2-Status-Watch/releases/latest). Install it on the phone, open **Connection**, and enter the same Web App address, username, and password used in a browser. For example, use `http://SERVER-IP:3274` while the phone is on your home network.

The app saves the server address and username on the device and protects the saved password with Android Keystore. The Web App must be reachable from the phone. Use HTTPS when connecting over an untrusted network; Android's saved-password protection does not encrypt the password while it travels over plain HTTP.

## Stream Deck plugin

The Windows Stream Deck plugin (version 1.0.2.0) displays the Web App's current status and lets you run **Check now** from a key. Download `thedivision2statuswatch.streamdeck.streamDeckPlugin` from the [GitHub Releases page](https://github.com/marxdrive/The-Division-2-Status-Watch/releases/latest) and open it to install. It requires Stream Deck 7.1 or newer.

Add **The Division 2 Status Watch → Server Status** to a key. In its settings, enter the Web App address and WEB_USERNAME/WEB_PASSWORD, choose a 15, 30, or 60 second display refresh, use **Test connection**, and save. The refresh only reads the Web App's current status; it does not change the Web App's Ubisoft check interval. Pressing the key runs a real status check and may send a notification if that check finds a selected status change. The plugin needs network access to the Web App from the computer running Stream Deck. Use HTTPS over untrusted networks.

## Gotify push notifications

Choose **Gotify push**, enter your Gotify server URL, and paste an application token from an application created in Gotify. Select **Send test push** to check the setup before saving; the test uses the values currently entered and does not change your saved settings. If the token field is blank, the saved token is used. The app posts selected status notifications to Gotify's message endpoint with the token in the X-Gotify-Key header. You can use a server URL with a reverse-proxy base path, or paste a URL ending in /message. Gotify priority ranges from 0 to 10; the default of 5 produces a sound in the Gotify Android app. Install and connect the Gotify app on your phone to receive push notifications.

See Gotify's [push message instructions](https://gotify.net/docs/pushmsg) and [priority guide](https://gotify.net/docs/priority) for creating application tokens and choosing notification behaviour.

## Email notifications

Use your mail provider's SMTP server, port, and connection security setting. Common combinations are STARTTLS on port 587 or SSL/TLS on port 465. Enter the sender address accepted by that provider. An SMTP app password may be required instead of the account password.

## WhatsApp notifications

WhatsApp alerts use Meta's WhatsApp Cloud API directly. In the app, enter your international WhatsApp number, the WhatsApp Phone Number ID from your Meta developer dashboard, and a Meta access token with WhatsApp Business messaging permission.

The app sends approved template messages. Create and approve a template in your WhatsApp Business account, then enter its name and language code (for example, `en_US`). Its body must have two placeholders: `{{1}}` for the reported status and `{{2}}` for the Ubisoft status page URL. See Meta's [Cloud API overview](https://developers.facebook.com/documentation/business-messaging/whatsapp/overview/) and [template documentation](https://developers.facebook.com/docs/graph-api/reference/whats-app-business-account/message_templates/).

When upgrading from the previous WhatsApp setup, the saved recipient number is kept, but the old provider credentials are removed. Enter the Meta Phone Number ID, access token, and approved template details before selecting WhatsApp and saving.

## Behavior

- The first successful check records a baseline and does not send an alert.
- Select any combination of Online (Operational), Maintenance, Problems, and Outage. A notification is sent once when Ubisoft changes to a selected status; staying in that status does not repeat the alert.
- Online (Operational) is selected by default, matching the previous recovery notification behaviour.
- WhatsApp delivery uses Meta's Cloud API directly and sends approved template messages.
- Temporary fetch failures show as Unknown; when no unambiguous live status is found, the dashboard displays Online as its default. Neither case counts as a status change.
- Notification credentials are stored in the persistent Docker volume and are not returned to the browser after saving.
- The monitor reports what Ubisoft's page publishes; it cannot verify connectivity from a particular player's device or platform.

