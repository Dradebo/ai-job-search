# Hosted API

This FastAPI service provides the private profile, document, saved-role, application-draft, and job-discovery endpoints used by the PWA in the separate `Dradebo/nextjs-boilerplate` repository.

## Local checks

From this directory, install `requirements.txt` and run:

```sh
python -m unittest discover -s tests -v
```

The suite uses synthetic applicants and postings across public health, technology, education, and finance in the United States, United Kingdom, Kenya, and Canada. It makes no network requests.

## Discovery scope

Matching is driven by the user's role, optional industry, location, and work-mode preferences. Empty location or industry preferences do not impose a U.S.-only or healthcare-only restriction. Existing `remote-US` profile values remain supported.

The current automated feeds are Jobicy, Himalayas, Remotive, and a limited Greenhouse board list read from the mounted CareerOps example configuration. That example list is AI/technology-heavy and is not a comprehensive job index; use a broader, maintained source registry before promising coverage for any specific occupation or country. Search links remain available as additional sources.

There is no public registration endpoint. The API remains bootstrap-account/invite controlled, and applications are prepared as drafts for human review; it does not submit applications or send email automatically.
