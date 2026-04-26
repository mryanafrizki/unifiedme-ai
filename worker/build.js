/**
 * Build script: embeds admin.html + install.sh into index.js.
 * Run: node build.js
 */
const fs = require('fs');
const path = require('path');

const indexPath = path.join(__dirname, 'src', 'index.js');
const htmlPath = path.join(__dirname, 'src', 'admin.html');
const installPath = path.join(__dirname, '..', 'semua-file', 'fix-unpath', 'install.sh');
const outPath = path.join(__dirname, 'dist', 'index.js');

let indexSrc = fs.readFileSync(indexPath, 'utf-8');
const htmlSrc = fs.readFileSync(htmlPath, 'utf-8');

// Escape backticks and ${} for template literals
const escapeForTemplate = (s) => s.replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$\{/g, '\\${');

let combined = indexSrc;

// Embed ADMIN_HTML
combined += '\n\n// ─── Embedded Admin Panel HTML ──────────────────────────────────────────────\nconst ADMIN_HTML = `' + escapeForTemplate(htmlSrc) + '`;\n';

// Embed INSTALL_SH
if (fs.existsSync(installPath)) {
    const installSrc = fs.readFileSync(installPath, 'utf-8');
    combined += '\n// ─── Embedded Install Script ────────────────────────────────────────────────\nconst INSTALL_SH = `' + escapeForTemplate(installSrc) + '`;\n';
    console.log('Embedded install.sh');
} else {
    combined += '\nconst INSTALL_SH = "# Install script not found";\n';
    console.log('WARNING: install.sh not found at', installPath);
}

fs.mkdirSync(path.join(__dirname, 'dist'), { recursive: true });
fs.writeFileSync(outPath, combined, 'utf-8');
console.log(`Built: ${outPath} (${(combined.length / 1024).toFixed(1)} KB)`);
