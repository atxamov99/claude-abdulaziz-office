// Suppress the console window in Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    claude_abdulaziz_office_lib::run();
}
