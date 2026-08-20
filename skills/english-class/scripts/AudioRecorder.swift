// 화상 수업 오디오 녹음기.
// ScreenCaptureKit으로 시스템 오디오(강사)와 마이크(나)를 각각 별도 파일에 담는다.
// 헤드셋을 써도 강사 목소리가 시스템 오디오 경로로 잡히고, 트랙이 나뉘어 화자 구분이 확실하다.
// 녹음 중에는 메뉴 막대에 경과 시간과 양쪽 입력 레벨을 실시간으로 띄운다.
//
//   audio-recorder <출력경로프리픽스> [--no-menubar]
//     → <프리픽스>-tutor.m4a (시스템 오디오), <프리픽스>-me.m4a (마이크)
//   SIGINT/SIGTERM 을 받으면 파일을 정상 마무리하고 종료한다.

import Foundation
import ScreenCaptureKit
import AVFoundation
import AppKit

/// 최근 입력 레벨(0~1). 표시용이라 정확한 계측이 아니라 눈에 보이는 반응성이 목적이다.
final class Meter: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Float = 0

    /// 올라갈 때는 즉시, 내려갈 때는 천천히 — 그래야 말이 끊겨도 막대가 깜빡이지 않는다.
    func feed(_ level: Float) {
        lock.lock()
        value = level > value ? level : value * 0.75 + level * 0.25
        lock.unlock()
    }

    var current: Float {
        lock.lock(); defer { lock.unlock() }
        return value
    }

    /// 8단계 블록 문자 3칸. 고정폭이라 메뉴 막대가 흔들리지 않는다.
    var bars: String {
        let db = 20 * log10(max(current, 1e-7))          // -140 ~ 0
        let norm = max(0, min(1, (db + 55) / 55))        // -55dB 이하는 무음 취급
        // 무음이라도 눈금은 남긴다 — 아무것도 안 보이면 녹음이 죽은 건지 조용한 건지 알 수 없다.
        let blocks = Array("▁▁▂▃▄▅▆▇█")
        return (0..<3).map { i -> String in
            let seg = max(0, min(1, (norm - Float(i) / 3) * 3))
            return String(blocks[Int(seg * 8)])
        }.joined()
    }
}

/// 두 트랙이 공유하는 세션 원점. 먼저 도착한 샘플의 PTS 가 원점이 된다.
/// 트랙마다 첫 샘플이 도착하는 시각이 다르므로(마이크 워밍업 등), 그 차이를
/// 여기서 재두지 않으면 두 파일이 조용히 어긋난 채로 남는다.
final class SessionClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value: CMTime?

    /// 원점을 확정하고(최초 1회) 그 값을 돌려준다.
    func resolve(first pts: CMTime) -> CMTime {
        lock.lock(); defer { lock.unlock() }
        if let v = value { return v }
        value = pts
        return pts
    }

    var origin: CMTime? {
        lock.lock(); defer { lock.unlock() }
        return value
    }
}

final class TrackWriter {
    /// 트랙 한 개의 녹음 결과. 종료 시 경고와 sync.json 에 쓰인다.
    struct Stats {
        let label: String
        let file: String
        let offset: Double     // 세션 원점 대비 이 트랙이 늦게 시작한 양(초)
        let duration: Double   // 첫 샘플 ~ 마지막 샘플
        let appended: Int
        let dropped: Int
    }

    private let writer: AVAssetWriter
    private let input: AVAssetWriterInput
    private var started = false
    private let queue = DispatchQueue(label: "trackwriter")
    private let clock: SessionClock
    private var firstPTS: CMTime?
    private var lastPTS: CMTime?
    private var appended = 0
    private var dropped = 0
    let url: URL
    let label: String
    let meter = Meter()

    init(url: URL, label: String, clock: SessionClock) throws {
        self.url = url
        self.label = label
        self.clock = clock
        try? FileManager.default.removeItem(at: url)
        writer = try AVAssetWriter(outputURL: url, fileType: .m4a)
        input = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48000,
            AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey: 64000,
        ])
        input.expectsMediaDataInRealTime = true
        writer.add(input)
    }

    func append(_ sample: CMSampleBuffer) {
        meter.feed(TrackWriter.rms(sample))
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        // 어느 트랙이 먼저 오든 원점은 한 번만 정해진다.
        _ = clock.resolve(first: pts)
        queue.sync {
            if !started {
                guard writer.startWriting() else { return }
                // 파일의 t=0 은 이 트랙의 첫 샘플이다 — 앞에 빈 구간을 만들지 않아야
                // ffmpeg/whisper 가 편집 목록을 어떻게 해석하든 결과가 흔들리지 않는다.
                // 두 트랙이 어긋난 양은 sync.json 에 남겨 병합 단계에서 보정한다.
                writer.startSession(atSourceTime: pts)
                firstPTS = pts
                started = true
            }
            lastPTS = pts
            if input.isReadyForMoreMediaData {
                input.append(sample)
                appended += 1
            } else {
                // 인코더가 밀리면 이 샘플은 사라진다. 조용히 넘기면 트랙만 짧아지고
                // 그 뒤가 통째로 밀리므로, 최소한 몇 개를 버렸는지는 세어 둔다.
                dropped += 1
            }
        }
    }

    var stats: Stats {
        queue.sync {
            let origin = clock.origin
            let offset: Double
            if let f = firstPTS, let o = origin { offset = (f - o).seconds } else { offset = 0 }
            let duration: Double
            if let f = firstPTS, let l = lastPTS { duration = (l - f).seconds } else { duration = 0 }
            return Stats(label: label, file: url.lastPathComponent,
                         offset: offset.isFinite ? offset : 0,
                         duration: duration.isFinite ? duration : 0,
                         appended: appended, dropped: dropped)
        }
    }

    func finish() async {
        let needsFinish: Bool = queue.sync {
            guard started else { return false }
            input.markAsFinished()
            return true
        }
        guard needsFinish else {
            FileHandle.standardError.write("경고: \(url.lastPathComponent) 에 담긴 오디오가 없다\n".data(using: .utf8)!)
            return
        }
        await writer.finishWriting()

        let s = stats
        if s.offset > 0.5 {
            warn("경고: \(s.label) 트랙이 다른 트랙보다 \(String(format: "%.1f", s.offset))초 늦게 시작했다 "
                 + "— \(s.file) 의 시각은 그만큼 당겨져 있다(병합 때 sync.json 으로 보정된다)")
        }
        if s.dropped > 0 {
            warn("경고: \(s.label) 트랙에서 샘플 \(s.dropped)개를 놓쳤다 "
                 + "(기록 \(s.appended)개) — 인코더가 밀렸다. 그만큼 이 트랙이 짧아졌다")
        }
    }

    private func warn(_ msg: String) {
        FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    }

    private static func rms(_ sample: CMSampleBuffer) -> Float {
        var sum: Float = 0
        var count = 0
        do {
            try sample.withAudioBufferList { list, _ in
                for buffer in list {
                    guard let data = buffer.mData else { continue }
                    let n = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
                    let ptr = data.bindMemory(to: Float.self, capacity: n)
                    for i in 0..<n { sum += ptr[i] * ptr[i] }
                    count += n
                }
            }
        } catch {
            return 0
        }
        return count > 0 ? sqrt(sum / Float(count)) : 0
    }
}

/// 신호 처리 스레드와 종료 처리 사이의 플래그.
final class Stopping: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false
    var requested: Bool { lock.lock(); defer { lock.unlock() }; return flag }
    func request() { lock.lock(); flag = true; lock.unlock() }
}

final class Output: NSObject, SCStreamOutput, SCStreamDelegate {
    let tutor: TrackWriter
    let me: TrackWriter

    init(tutor: TrackWriter, me: TrackWriter) {
        self.tutor = tutor
        self.me = me
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard CMSampleBufferDataIsReady(buffer) else { return }
        switch type {
        case .audio: tutor.append(buffer)       // 시스템 오디오 = 상대방 목소리
        case .microphone: me.append(buffer)     // 마이크 = 내 목소리
        default: break                          // .screen 은 등록하지 않는다
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("스트림 중단: \(error.localizedDescription)\n".data(using: .utf8)!)
        exit(1)
    }
}

/// 최근 입력 레벨을 음파 파형으로 그린다. 오른쪽이 현재.
@MainActor
final class WaveformView: NSView {
    var samples: [Float] = []
    var color: NSColor = .controlAccentColor

    override func draw(_ dirtyRect: NSRect) {
        let mid = bounds.midY
        let barW: CGFloat = 2, gap: CGFloat = 2
        let count = max(1, Int(bounds.width / (barW + gap)))

        // 바탕 기준선 — 신호가 없어도 파형 영역이 어디인지 보이게 한다
        color.withAlphaComponent(0.15).setFill()
        NSBezierPath(rect: NSRect(x: 0, y: mid - 0.5, width: bounds.width, height: 1)).fill()

        color.setFill()
        let recent = samples.suffix(count)
        let offset = bounds.width - CGFloat(recent.count) * (barW + gap)
        for (i, level) in recent.enumerated() {
            // 레벨은 진폭이 작은 구간에 몰려 있어 그대로 그리면 거의 평평하다. 로그로 편다.
            let db = 20 * log10(max(level, 1e-7))
            let norm = CGFloat(max(0, min(1, (db + 55) / 55)))
            let h = max(1.5, norm * bounds.height * 0.9)
            let x = offset + CGFloat(i) * (barW + gap)
            let rect = NSRect(x: x, y: mid - h / 2, width: barW, height: h)
            NSBezierPath(roundedRect: rect, xRadius: barW / 2, yRadius: barW / 2).fill()
        }
    }
}

/// 메뉴 막대 아이콘 하나. 클릭하면 화자별 파형이 담긴 팝오버가 열린다.
@MainActor
final class StatusBar {
    private let item: NSStatusItem
    private let tutor: Meter
    private let me: Meter
    private let started = Date()
    private var timer: Timer?
    private var onStop: (() -> Void)?

    private let popover = NSPopover()
    private let clockLabel = NSTextField(labelWithString: "0:00")
    private let tutorWave = WaveformView()
    private let meWave = WaveformView()

    init(tutor: Meter, me: Meter, onStop: @escaping () -> Void) {
        self.tutor = tutor
        self.me = me
        self.onStop = onStop

        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.target = self
        item.button?.action = #selector(togglePopover)

        buildPopover()
        updateIcon(level: 0)

        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
    }

    private func buildPopover() {
        let width: CGFloat = 260, height: CGFloat = 194
        let root = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))

        func caption(_ text: String, _ y: CGFloat, size: CGFloat = 11, color: NSColor = .secondaryLabelColor) -> NSTextField {
            let label = NSTextField(labelWithString: text)
            label.font = .systemFont(ofSize: size, weight: .medium)
            label.textColor = color
            label.frame = NSRect(x: 16, y: y, width: width - 32, height: size + 5)
            return label
        }

        let title = caption("녹음 중", height - 32, size: 13, color: .labelColor)
        root.addSubview(title)

        clockLabel.font = .monospacedDigitSystemFont(ofSize: 13, weight: .regular)
        clockLabel.textColor = .secondaryLabelColor
        clockLabel.alignment = .right
        clockLabel.frame = NSRect(x: width - 90, y: height - 32, width: 74, height: 18)
        root.addSubview(clockLabel)

        root.addSubview(caption("강사", height - 60))
        tutorWave.frame = NSRect(x: 16, y: height - 92, width: width - 32, height: 30)
        tutorWave.color = .controlAccentColor
        root.addSubview(tutorWave)

        root.addSubview(caption("나", height - 118))
        meWave.frame = NSRect(x: 16, y: height - 152, width: width - 32, height: 30)
        meWave.color = .systemGray
        root.addSubview(meWave)

        let stop = NSButton(title: "녹음 중지", target: self, action: #selector(stopClicked))
        stop.bezelStyle = .rounded
        stop.frame = NSRect(x: 16, y: 10, width: width - 32, height: 24)
        root.addSubview(stop)

        let vc = NSViewController()
        vc.view = root
        popover.contentViewController = vc
        popover.contentSize = NSSize(width: width, height: height)
        popover.behavior = .transient
    }

    @objc private func togglePopover() {
        if popover.isShown {
            popover.performClose(nil)
        } else if let button = item.button {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    /// 테스트에서 팝오버 모습을 확인할 때 쓴다.
    func openPopover() { if !popover.isShown { togglePopover() } }

    @objc private func stopClicked() {
        popover.performClose(nil)
        onStop?()
    }

    private func tick() {
        let t = tutor.current, m = me.current
        tutorWave.samples.append(t)
        meWave.samples.append(m)
        // 표시 폭보다 길게 쌓아둘 이유가 없다
        if tutorWave.samples.count > 200 {
            tutorWave.samples.removeFirst(tutorWave.samples.count - 200)
            meWave.samples.removeFirst(meWave.samples.count - 200)
        }

        updateIcon(level: max(t, m))

        if popover.isShown {
            let sec = Int(Date().timeIntervalSince(started))
            clockLabel.stringValue = sec >= 3600
                ? String(format: "%d:%02d:%02d", sec / 3600, sec % 3600 / 60, sec % 60)
                : String(format: "%d:%02d", sec / 60, sec % 60)
            tutorWave.needsDisplay = true
            meWave.needsDisplay = true
        }
    }

    /// SF Symbol 의 variable value 로 아이콘 자체가 입력 레벨에 반응한다.
    private func updateIcon(level: Float) {
        let db = 20 * log10(max(level, 1e-7))
        let norm = Double(max(0, min(1, (db + 55) / 55)))
        let image = NSImage(systemSymbolName: "waveform",
                            variableValue: norm,
                            accessibilityDescription: "영어 수업 녹음 중")
        image?.isTemplate = false
        item.button?.image = image?.withSymbolConfiguration(
            NSImage.SymbolConfiguration(paletteColors: [.systemRed]))
    }

    func teardown() {
        timer?.invalidate()
        popover.performClose(nil)
        NSStatusBar.system.removeStatusItem(item)
    }
}

@main
struct Main {
    static func main() {
        let args = CommandLine.arguments
        guard args.count >= 2 else {
            FileHandle.standardError.write("사용법: audio-recorder <출력경로프리픽스> [--no-menubar]\n".data(using: .utf8)!)
            exit(2)
        }
        let prefix = args[1]
        let showMenuBar = !args.contains("--no-menubar")

        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)   // Dock 아이콘 없이 메뉴 막대에만 뜬다

        let stopping = Stopping()
        var statusBar: StatusBar?
        var teardown: (@Sendable () -> Void)?

        Task { @MainActor in
            do {
                let clock = SessionClock()
                let tutor = try TrackWriter(url: URL(fileURLWithPath: "\(prefix)-tutor.m4a"),
                                            label: "강사", clock: clock)
                let me = try TrackWriter(url: URL(fileURLWithPath: "\(prefix)-me.m4a"),
                                         label: "나", clock: clock)
                let output = Output(tutor: tutor, me: me)

                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                guard let display = content.displays.first else {
                    FileHandle.standardError.write("디스플레이를 찾지 못했다\n".data(using: .utf8)!)
                    exit(1)
                }

                // 오디오만 필요하다. 화면은 최소 크기로 받고 .screen 출력은 등록하지 않는다.
                let config = SCStreamConfiguration()
                config.capturesAudio = true
                config.captureMicrophone = true
                config.sampleRate = 48000
                config.channelCount = 2
                config.width = 2
                config.height = 2
                config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
                config.showsCursor = false

                let filter = SCContentFilter(display: display, excludingWindows: [])
                let stream = SCStream(filter: filter, configuration: config, delegate: output)
                let queue = DispatchQueue(label: "capture")
                try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: queue)
                try stream.addStreamOutput(output, type: .microphone, sampleHandlerQueue: queue)
                try await stream.startCapture()
                Main.retained = [stream, output, tutor, me]

                print("recording")
                fflush(stdout)

                // 파일을 정상 마무리하고 끝낸다. 그냥 죽이면 m4a 가 깨진다.
                let finish: @Sendable () -> Void = {
                    Task { @MainActor in
                        statusBar?.teardown()
                        try? await stream.stopCapture()
                        await tutor.finish()
                        await me.finish()
                        Main.writeSync(prefix: prefix, tracks: [tutor.stats, me.stats])
                        print("stopped")
                        exit(0)
                    }
                }
                teardown = finish

                if args.contains("--debug-level") {
                    Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in
                        let msg = String(format: "level tutor=%.5f me=%.5f\n", tutor.meter.current, me.meter.current)
                        FileHandle.standardError.write(msg.data(using: .utf8)!)
                    }
                }
                if showMenuBar {
                    let bar = StatusBar(tutor: tutor.meter, me: me.meter, onStop: { finish() })
                    statusBar = bar
                    Main.retained.append(bar)
                    if args.contains("--show-popover") {   // 표시 확인용
                        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: false) { _ in
                            Task { @MainActor in bar.openPopover() }
                        }
                    }
                }

                // 신호는 런루프 밖에서 오므로 플래그를 폴링해 메인에서 처리한다.
                Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { t in
                    if stopping.requested {
                        t.invalidate()
                        finish()
                    }
                }
            } catch {
                FileHandle.standardError.write("녹음 시작 실패: \(error.localizedDescription)\n".data(using: .utf8)!)
                exit(1)
            }
        }

        for sig in [SIGINT, SIGTERM] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .global())
            src.setEventHandler { stopping.request() }
            src.resume()
            sources.append(src)
        }

        withExtendedLifetime(teardown) {}
        app.run()
    }

    /// 신호 소스는 살려둬야 한다. 해제되면 신호를 못 받는다.
    nonisolated(unsafe) static var sources: [DispatchSourceSignal] = []

    /// 스트림과 라이터도 마찬가지다. Task 블록이 끝날 때 해제되면 캡처가 조용히 멈춘다.
    nonisolated(unsafe) static var retained: [AnyObject] = []

    /// 트랙별 시작 지연·유실을 <프리픽스>-sync.json 에 남긴다.
    /// 각 m4a 의 t=0 은 그 트랙의 첫 샘플이므로, 병합 단계에서 offset 을 더해야
    /// 두 트랙의 시각이 같은 기준 위에 놓인다.
    static func writeSync(prefix: String, tracks: [TrackWriter.Stats]) {
        var payload: [String: Any] = ["version": 1]
        for t in tracks {
            let key = t.file.hasSuffix("-tutor.m4a") ? "tutor" : "me"
            payload[key] = [
                "file": t.file,
                "label": t.label,
                "offset": t.offset,       // 세션 원점 대비 시작 지연(초)
                "duration": t.duration,
                "appended": t.appended,
                "dropped": t.dropped,
            ]
        }
        guard let data = try? JSONSerialization.data(withJSONObject: payload,
                                                     options: [.prettyPrinted, .sortedKeys]) else { return }
        try? data.write(to: URL(fileURLWithPath: "\(prefix)-sync.json"))
    }
}
