# Data Model — Dark Mode for the Reader

## Reader

- **id** (uuid): primary key
- **email** (string): login identity
- **created_at** (timestamp): signup time

## ThemePreference

- **id** (uuid): primary key
- **reader_id** (uuid): owning reader
- **theme** (string): one of light, dark or system

## Workspace

- **id** (uuid): tenant identifier
- **default_theme** (string): fallback theme for new readers

A Reader has many ThemePreference rows. A ThemePreference belongs to a Workspace.
