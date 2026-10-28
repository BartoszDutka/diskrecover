# Contributing

Keep changes focused and preserve the read-only design. New filesystem
support must have parser tests using synthetic images and must report an
unsupported format explicitly when detection is uncertain.

Run `pytest -q` before submitting a pull request. Do not add real disk images,
personal documents, credentials, or user-specific paths to the repository.
