# deploy/home — the home.memention.net site

The owner's concrete deployment (the reference install of the generic recipe
in `../README.md`). Site facts:

- **Host:** `homepi` (Raspberry Pi 5, Raspbian), ssh alias **`home`**,
  service user `epatel` (holds the `claude` login).
- **Layout:** code `~/dev-lab/claude-agent-team/`, projects `~/dev-lab/labs/`,
  `CLIENT_TOKEN` in `dev-lab/.env` (chmod 600, never synced).
- **Service:** `dev-lab-web.service` (this directory's copy is what's installed
  at `/etc/systemd/system/`) — loopback :8770.
- **Apache:** the stock `../apache-dev-lab.conf` included in the
  `000-default-le-ssl.conf` *:443 vhost (Let's Encrypt), plus in the *:80 vhost:
  `RedirectMatch permanent ^/dev-lab(.*)$ https://home.memention.net/dev-lab$1`.
  Pre-change backups: `/etc/apache2/*.bak-devlab`.
- **URL:** https://home.memention.net/dev-lab/ — platform clients dial
  `wss://home.memention.net/dev-lab/ws/client` with the token.

## Deploy / update

```sh
deploy/home/deploy.sh    # rsync working tree → restart service → health check
```

Starting another site? Copy this directory, swap the user/paths in the unit,
the host in the redirect, and the ssh alias in `deploy.sh`.
