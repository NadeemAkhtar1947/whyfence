# Working on stale-repo

Always run `npm run lint` before committing, then `npm run test:unit`.

```bash
npm run build
cd web && npm run start
make -C api tests
make migrate
```

To deploy: `make deploy` (needs AWS creds).
