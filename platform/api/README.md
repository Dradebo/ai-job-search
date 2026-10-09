# Hosted API

This FastAPI service provides the profile, document, saved-role, application-draft, and job-discovery endpoints used by the PWA in the separate `Dradebo/nextjs-boilerplate` repository.

## Local checks

From this directory, install `requirements.txt` and run:

```sh
python -m unittest discover -s tests -v
```

The synthetic suite covers public health, healthcare, technology, education, and finance roles across the United States, United Kingdom, Kenya, Uganda, and Canada. It makes no network requests.

## Matching and source coverage

Matching is profile-driven: roles, optional industries, locations, and work modes are applied to listings from every provider. Empty industry or location preferences do not impose healthcare or U.S.-only filters. Public-health matching recognizes closely related work such as epidemiology, biostatistics, population health, and disease surveillance. Industry aliases are hints; applicants should still review each posting and its eligibility requirements.

The default automated feeds (Jobicy, Himalayas, Remotive) are remote-only and are not a comprehensive job index. Greenhouse boards are read only from the explicit `GREENHOUSE_BOARDS` environment variable (comma-separated board slugs); the CareerOps example config is deliberately not used as a production catalog. USAJOBS matching is enabled only when both `USAJOBS_API_KEY` and `USAJOBS_USER_AGENT` are configured. See `.env.example` and the [official USAJOBS API reference](https://developer.usajobs.gov/api-reference/job-apis). Google Jobs and LinkedIn search links remain available as broader discovery fallbacks.

This is an extensible foundation, not a promise to index every employer, occupation, country, or job board. Broader coverage requires selecting compliant feeds or maintaining additional source adapters and, where required, obtaining the providers' credentials.

## Invite-only accounts

There is no public self-registration. The bootstrap account (or an address explicitly listed in `INVITER_EMAILS`) can create an invitation from the PWA. Links are single-use, expire after seven days by default, and are bound to the email entered by the inviter. The PWA sends no email; share the generated link directly and privately. The token is carried in the URL fragment and only its hash is stored by the API.

Set `APP_ORIGIN` to the exact PWA origin for CORS and invite-origin checks. Set `INVITE_BASE_URL` to the PWA's public base URL (it falls back to the first `APP_ORIGIN`). Leave `INVITER_EMAILS` empty to allow only `BOOTSTRAP_EMAIL` to invite. New accounts start with a blank profile and their own user-scoped saved roles, application drafts, and documents. Passwords must be at least 12 characters.

The invite is a bearer link: anyone who receives it can claim it for the email it names, so do not forward it. An owner can issue a replacement link, which invalidates the previous unused link.

Applications are prepared as drafts for human review; the service does not send email or submit applications automatically. The changes are not deployed to Phoenix until separately authorized.