# 0001. One core package, server deployments install it

Status: accepted, 1.1.0

## Context

This package started as the public version of two private tools I run on my own VPS: moodle-sync (Moodle sync, a remote MCP server, a small website) and ed-digest (Ed digests). Each of them kept its own copy of the Moodle client, the page parsers, the sync code, the search index and the Ed client.

By October 2026 the copies had drifted both ways. Zip search and `since_days` only existed on the server. Windows-safe file names, the timezone setting and keeping replies under deleted Ed replies only existed here. Every fix had to be found and made two or three times, and some were missed.

## Decision

`monash-study-kit` is the only copy of the shared code. The server tools install it as a dependency with a fixed version and keep only what runs on the server: the remote MCP server and its OAuth, signed download links, the website, submitting work, systemd timers and the Ed digest pages.

Server-only behaviour is a setting, not a fork:

- paths: `MONASH_KIT_DATA`, `MONASH_KIT_SECRETS`, `MONASH_KIT_ED_DATA`, `MONASH_KIT_ED_TOKEN`;
- choices: `name_limit`, `pdf_extractor`, `download_videos`, each also settable as `MONASH_KIT_<NAME>`.

Defaults stay what a student on a laptop needs.

## Consequences

- A fix lands once and reaches both my server and everyone using the package.
- The code I use every day is the code other people install, so bugs show up for me first.
- The server tools can't change shared code directly. They need a release of this package first, then a version bump on their side.
- Settings read at import time (paths) must be set before `monash_study_kit` is imported. The server tools do this in one small module that every entry point imports first.
