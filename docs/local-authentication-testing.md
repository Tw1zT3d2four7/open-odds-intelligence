# Local authentication testing

Authentication behavior is validated by the repository's automated test suite and the controlled checks in [TESTING_PLAN.md](TESTING_PLAN.md). This guide does not define a second deployment stack.

## One Compose file and one environment file

Use the root `docker-compose.yml` and the same private root `.env` used by the selected image channel. The Compose file defaults to `:latest`; its commented `:dev` image line is the only alternative. Switching channels means changing the image line in that one file, then pulling and recreating the same service. Do not run both images concurrently against the same `./data` directory.

The repository contains one safe environment template, `.env.example`. Copy it to `.env` once on the host and keep that file private and untracked. Do not create or commit `.env.local`, `.env.staging`, or additional environment templates. Never copy production accounts, MFA seeds, session data, or provider secrets into an isolated test environment.

## Local-only authentication policy

For automated tests, use the isolated fixtures and test configuration already defined under `tests/`; do not change the live server's `.env` to simulate a development profile. If manually exercising development-only authentication behavior, do so on a disposable workstation/checkout, using the same single Compose file and a disposable private `.env` and data directory. Bind any local-only test listener to loopback and never expose the development profile through a public hostname or reverse proxy.

The development email transports (`local_outbox` and `disabled`) are test-only. The local outbox is read through the trusted local CLI; its one-time links are secrets and must not be copied into logs or shared. Staging and production must retain email verification, administrator MFA, rate limiting, CSRF, and the other fail-closed security invariants.

See [authentication environment profiles](authentication-environment-profiles.md), [identity staging acceptance](identity-staging-acceptance-checklist.md), and [production operations](PRODUCTION_OPERATIONS.md) for the policy boundary and acceptance requirements.
