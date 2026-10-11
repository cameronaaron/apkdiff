//! Fast helpers for `apkdiff.sh`.
//!
//! Two jobs live here, both previously done by slower means:
//!
//! * [`native`] diffs the log strings in a stripped native library. The Python
//!   version of this was ~64% of total runtime.
//! * [`dex`] and [`dexdiff`] read the Dalvik format directly, so stage 3 no
//!   longer has to shell out to apktool and diff disassembled smali text to
//!   learn which classes, members and strings changed.
//!
//! No external crates: every pattern involved is simple enough to express
//! directly, which keeps a cold build to a couple of seconds.

pub mod dex;
pub mod dexdiff;
pub mod native;
pub mod testdex;
