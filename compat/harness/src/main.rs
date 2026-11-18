//! Checks one setup folder the way `cmux setup install` does, with the cmux
//! code of the commit this binary is built from (compat/compat.py builds it
//! against a nightly's source).
//!
//!     cmux-compat-check RUNNING_VERSION OWNER DIR
//!
//! Prints one JSON object: `ok`, the setup's `id`, and `findings`, each with
//! `stage` (read, settings, plan, install), `code`, `key` (a setting, when
//! the finding names one) and `message`. The settings stage lists every
//! setting the schema refuses; install stops at the first. Install runs
//! into the HOME this process is given, which compat.py makes empty for each
//! repo: it refuses to run without CMUX_COMPAT_ISOLATED=1.

use std::path::Path;

use cmux_config::Schema;
use cmux_config::schema::{accepts, replaced_keys, retired_reason};
use cmux_customize::{Customize, Roots, setup};
use serde_json::{Value, json};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [running, owner, dir] = args.as_slice() else {
        eprintln!("usage: cmux-compat-check RUNNING_VERSION OWNER DIR");
        std::process::exit(64);
    };
    if std::env::var("CMUX_COMPAT_ISOLATED").as_deref() != Ok("1") {
        eprintln!("refusing: set CMUX_COMPAT_ISOLATED=1 and an empty HOME (install writes there)");
        std::process::exit(64);
    }
    let Some(roots) = Roots::from_env() else {
        eprintln!("refusing: no HOME");
        std::process::exit(64);
    };
    let report = check(Customize::open(roots), Path::new(dir), owner, running);
    println!("{report}");
}

fn finding(stage: &str, code: &str, key: Option<&str>, message: String) -> Value {
    json!({"stage": stage, "code": code, "key": key, "message": message})
}

fn check(customize: Customize, dir: &Path, owner: &str, running: &str) -> Value {
    let read = match setup::read_dir(dir) {
        Ok(read) => read,
        Err(error) => {
            let findings = [finding("read", error.code(), None, error.to_string())];
            return json!({"ok": false, "id": null, "findings": findings});
        }
    };
    let mut findings = Vec::new();

    // Every setting, so one report names all of them (install stops at the first).
    let schema = Schema::embedded();
    for (key, value) in &read.profile.settings {
        let path: Vec<String> = key.split('.').map(str::to_owned).collect();
        let Some(row) = schema.row(key) else {
            let mut message = format!("the setting {key} is not one this cmux has");
            if let Some(new) = replaced_keys().iter().find(|replaced| replaced.old == path) {
                message.push_str(&format!("; it is now {}", new.row.join(".")));
            } else if let Some(reason) = retired_reason(&path) {
                message.push_str(&format!("; it was retired: {reason}"));
            }
            findings.push(finding("settings", "unknown_setting", Some(key), message));
            continue;
        };
        if !row.agent_settable {
            let message = format!("the setting {key} is one only the person can change");
            findings.push(finding("settings", "not_settable", Some(key), message));
        }
        if let Err(refusal) = accepts(row, value, &Default::default()) {
            let message = format!("the value for {key} is refused: {refusal}");
            findings.push(finding("settings", "value_refused", Some(key), message));
        }
    }

    // The install's own verdict: plan_setup is what `cmux setup install` shows
    // on its card, install_setup what it then applies.
    let plan = match customize.plan_setup(&read, owner, running) {
        Ok(plan) => serde_json::to_value(&plan).unwrap_or(Value::Null),
        Err(error) => {
            findings.push(finding("plan", error.code(), None, error.to_string()));
            Value::Null
        }
    };
    if !plan.is_null() {
        let label = format!("setup {} (compat check)", read.manifest.id);
        if let Err(error) = customize.install_setup(&read, &label, "compat", 0) {
            findings.push(finding("install", error.code(), None, error.to_string()));
        }
    }
    json!({
        "ok": findings.is_empty(),
        "id": read.manifest.id,
        "version": read.manifest.version,
        "extensions": read.manifest.extensions,
        "findings": findings,
        "plan": plan,
    })
}
