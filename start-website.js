// Launches the Nina-Website Next.js dev server from this project's root
const { spawn } = require('child_process')
const path = require('path')

const websiteDir = path.resolve(__dirname, '..', '..', 'Nina-Website')

const proc = spawn('npm', ['run', 'dev'], {
  cwd: websiteDir,
  stdio: 'inherit',
  shell: true,
})

proc.on('error', (err) => {
  console.error('Failed to start:', err)
  process.exit(1)
})

proc.on('exit', (code) => process.exit(code ?? 0))
