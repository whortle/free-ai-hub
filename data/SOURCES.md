# Data sources

Primary upstream:

- [freellmpool](https://github.com/0xzr/freellmpool), MIT.
  `freellmpool.providers.toml` supplies provider ids, base URLs, auth mode,
  model ids, context sizes, key env names and free-tier metadata.
  The license is reproduced at `data/licenses/freellmpool-LICENSE`.

Additional metadata snapshot:

- `free-rider.providers.json` supplies the broader provider directory (tier,
  signup URL, card requirement, free-model counts, rate-limit hints). It is
  kept with its upstream MIT license at `data/licenses/Free-rider-LICENSE`.

The running gateway reads these catalogs plus its own `data/settings.json`,
`data/keys.json` and explicitly named provider environment variables. It does
not import the Free-rider client-configuration scripts.

## Updating

Replace either file with a newer snapshot from its upstream project, then
restart the gateway. Provider and model ids are read at startup.
