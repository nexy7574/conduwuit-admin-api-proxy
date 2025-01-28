# conduwuit Admin API Proxy

An unofficial unfinished experimental API that proxies HTTP requests to the conduwuit admin room,
allowing you to perform admin actions over a REST API.

This is more of a proof of concept than anything, and may not be as functional as you would require for a production
deployment.

## Usage

You will need `fastapi[standard]`. You can install it with `pip install fastapi[standard]`.

You will also need three environment variables:

- `MATRIX_HOMESERVER` - The URL of the Matrix homeserver. For example, `https://matrix-client.matrix.org`.
- `ADMIN_ROOM_ID` - The room **ID** of the admin room. For example, `!abc123:matrix.org`.
- `ACCESS_TOKEN` - An access token to an account that is an admin of the server. It is recommended to use a dedicated
   account for this, as the API syncs in the background, and a large account may throttle the server.

Once these are set, you can then start the server with `fastapi run src`.

```shell
fastapi run --host 0.0.0.0 --port 8000 src
```

Note that API requests must also then be authenticated separately, however, this means that any administrator in the
conduwuit admin room can perform actions over the API. You should use your own account access token to make requests
to the API.
