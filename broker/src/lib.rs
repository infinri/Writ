//! Writ V2 authorization broker (M3): the only path to governed effects.
//!
//! The broker verifies sealed ingress, re-decides at use time through the pure
//! `writ-kernel`, consumes approval grants, issues and verifies permits, records
//! dispatch intent and receipts, and reconciles uncertain outcomes. Persistence
//! is behind [`store::LedgerStore`]; the SQLite local profile lives in
//! `writ-ledger-sqlite`. Plan: docs/implementation/M3-START-PLAN.md.

pub mod store;
