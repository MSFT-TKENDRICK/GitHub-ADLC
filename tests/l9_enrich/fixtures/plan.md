# Implementation Plan — Dark Mode for the Reader

## Components

Theme Service (web app) owns palette resolution. Preferences API (backend)
persists the choice. Settings Handler mediates between the two.

## Routes touched

- `/settings/appearance`
