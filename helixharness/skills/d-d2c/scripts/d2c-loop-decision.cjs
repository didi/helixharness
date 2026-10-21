#!/usr/bin/env node
/* eslint-disable no-console */

const choices = [
  { label: '继续优化', value: 'continue' },
  { label: '取消', value: 'cancel' },
]

// 支持 --choice <continue|cancel> 直传，跳过 TTY 交互
const choiceIdx = process.argv.indexOf('--choice')
if (choiceIdx !== -1) {
  const val = process.argv[choiceIdx + 1]
  if (val && ['continue', 'cancel'].includes(val)) {
    process.stdout.write(`${val}\n`)
    process.exit(0)
  }
  process.stderr.write(`无效的 --choice 值: ${val}，允许值: continue, cancel\n`)
  process.exit(1)
}

if (!process.stdin.isTTY || !process.stderr.isTTY) {
  process.stderr.write(
    '当前 shell 不支持交互选择，请在对话中明确回复：继续优化 或 取消。\n'
  )
  process.stdout.write('manual_required\n')
  process.exit(2)
}

let selected = 0

function render() {
  process.stderr.write('\x1b[2J\x1b[H')
  process.stderr.write('请选择下一步：\n\n')
  choices.forEach((choice, index) => {
    const pointer = index === selected ? '>' : ' '
    const checked = index === selected ? '[x]' : '[ ]'
    process.stderr.write(`${pointer} ${checked} ${choice.label}\n`)
  })
  process.stderr.write('\n使用 ↑/↓ 切换，Enter 确认。\n')
}

function cleanup() {
  process.stderr.write('\x1b[?25h')
  process.stdin.setRawMode(false)
  process.stdin.pause()
}

function finish(value) {
  cleanup()
  process.stderr.write('\n')
  process.stdout.write(`${value}\n`)
  process.exit(0)
}

process.stdin.setRawMode(true)
process.stdin.resume()
process.stdin.setEncoding('utf8')
process.stderr.write('\x1b[?25l')
render()

process.stdin.on('data', (key) => {
  if (key === '\u0003') {
    finish('cancel')
    return
  }

  if (key === '\r' || key === '\n') {
    finish(choices[selected].value)
    return
  }

  if (key === '\u001b[A' || key === 'k') {
    selected = (selected + choices.length - 1) % choices.length
    render()
    return
  }

  if (key === '\u001b[B' || key === 'j') {
    selected = (selected + 1) % choices.length
    render()
  }
})
