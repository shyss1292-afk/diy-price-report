// 「配件价格追踪」桌面应用 —— 基于系统原生 WKWebView（Objective-C）。
//
// 为什么不用 Chrome 套壳（scripts/make_desktop_app.sh 里那套）：
//   Chrome `--app` 会拉起一个**完整的 Chrome 实例**，实测吃 **660~783 MB**。
//   在 8G 机器上（还要和 WorkBuddy、日常浏览器分内存）这就是"卡"的来源。
//   系统 WebKit 的 XPC 服务只要 ~90 MB，小一个数量级。
//   本项目是**本地看板**，不需要 Chrome 的任何特性（扩展/同步/DevTools），
//   没理由为它养一个完整浏览器。
//
// 为什么是 Objective-C 而不是 Swift：
//   本机 CommandLineTools 坏了 —— usr/include/swift/ 下同时存在
//   module.modulemap(2023) 和 bridging.modulemap(2024)，两者都定义
//   `SwiftBridging`，swiftc 直接报 "redefinition of module"。
//   那是 root 拥有的系统文件，不动它；clang 不加载 Swift 的 modulemap，绕开。
//
// 编译：clang -fobjc-arc -framework Cocoa -framework WebKit -o 配件价格追踪 webview_app.m

#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

static NSString *const kTitle = @"配件价格追踪";
static NSString *const kBaseURL = @"http://127.0.0.1:8848";

@interface AppDelegate : NSObject <NSApplicationDelegate, WKNavigationDelegate>
@property (strong) NSWindow *window;
@property (strong) WKWebView *webView;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)note {
    [self buildMenu];
    [self buildWindow];
    [self waitForServiceThenLoad];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    return YES;
}

#pragma mark - 窗口

- (void)buildWindow {
    WKWebViewConfiguration *cfg = [[WKWebViewConfiguration alloc] init];
    // 持久化网站数据：缓存与 localStorage 都留下，第二次打开才快
    cfg.websiteDataStore = [WKWebsiteDataStore defaultDataStore];

    self.webView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:cfg];
    self.webView.navigationDelegate = self;
    self.webView.allowsMagnification = YES;
    self.webView.translatesAutoresizingMaskIntoConstraints = NO;

    self.window = [[NSWindow alloc]
        initWithContentRect:NSMakeRect(0, 0, 1180, 820)
                  styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                             NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                    backing:NSBackingStoreBuffered
                      defer:NO];
    self.window.title = kTitle;
    self.window.contentView = self.webView;
    [self.window center];
    [self.window setFrameAutosaveName:@"MainWindow"];   // 记住窗口位置与大小

    [NSLayoutConstraint activateConstraints:@[
        [self.webView.leadingAnchor constraintEqualToAnchor:self.window.contentView.leadingAnchor],
        [self.webView.trailingAnchor constraintEqualToAnchor:self.window.contentView.trailingAnchor],
        [self.webView.topAnchor constraintEqualToAnchor:self.window.contentView.topAnchor],
        [self.webView.bottomAnchor constraintEqualToAnchor:self.window.contentView.bottomAnchor],
    ]];

    // ⚠️ 必须显式显示窗口。`NSWindow` 创建出来是**隐藏**的 ——
    //    少这一行的话进程正常跑、内存正常、但屏幕上什么都没有，
    //    而且 AX 查询会报 "无效的索引"（窗口数为 0），极难一眼看出。
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

#pragma mark - 等后端就绪

/// 轮询 /api/health，最多等 45 秒。
///
/// 为什么必须等：服务由 launchd 托管、登录后自动启动，但刚开机那几十秒还没就绪。
/// 直接 load 会看到「无法连接」，用户以为应用坏了。
- (void)waitForServiceThenLoad {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:45];
    [self probeHealthUntil:deadline];
}

- (void)probeHealthUntil:(NSDate *)deadline {
    NSMutableURLRequest *req =
        [NSMutableURLRequest requestWithURL:[NSURL URLWithString:[kBaseURL stringByAppendingString:@"/api/health"]]];
    req.timeoutInterval = 2;
    req.cachePolicy = NSURLRequestReloadIgnoringLocalCacheData;

    __weak AppDelegate *weakSelf = self;
    NSURLSessionDataTask *task = [[NSURLSession sharedSession]
        dataTaskWithRequest:req
          completionHandler:^(NSData *data, NSURLResponse *resp, NSError *err) {
            BOOL ok = [(NSHTTPURLResponse *)resp statusCode] == 200;
            dispatch_async(dispatch_get_main_queue(), ^{
                AppDelegate *self_ = weakSelf;
                if (!self_) return;
                if (ok) {
                    [self_.webView loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:kBaseURL]]];
                } else if ([deadline timeIntervalSinceNow] > 0) {
                    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.5 * NSEC_PER_SEC)),
                                   dispatch_get_main_queue(), ^{ [self_ probeHealthUntil:deadline]; });
                } else {
                    // 服务没起来也要把窗口显示出来，并给出可操作的信息 ——
                    // 静默失败比报错更让人困惑
                    NSString *html = @"<html><head><meta charset='utf-8'><style>"
                        "body{font:15px -apple-system;display:flex;align-items:center;"
                        "justify-content:center;height:100vh;margin:0;color:#444}"
                        ".b{text-align:center;line-height:1.9}"
                        "code{background:#f2f2f7;padding:3px 7px;border-radius:5px;font-size:13px}"
                        "</style></head><body><div class='b'>"
                        "<h2>后台服务没有启动</h2>"
                        "<p>在终端执行下面这行，然后按 ⌘R 重试：</p>"
                        "<p><code>launchctl kickstart -k gui/$(id -u)/com.diyprice.tracker</code></p>"
                        "</div></body></html>";
                    [self_.webView loadHTMLString:html baseURL:nil];
                }
            });
          }];
    [task resume];
}

#pragma mark - 菜单

/// 没有菜单栏的话 ⌘Q / ⌘R 都不生效 —— 用户会觉得"这应用很怪"。
- (void)buildMenu {
    NSMenu *main = [[NSMenu alloc] init];

    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    [main addItem:appItem];
    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenu addItemWithTitle:[@"关于 " stringByAppendingString:kTitle]
                       action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:[@"隐藏 " stringByAppendingString:kTitle]
                       action:@selector(hide:) keyEquivalent:@"h"];
    [appMenu addItemWithTitle:[@"退出 " stringByAppendingString:kTitle]
                       action:@selector(terminate:) keyEquivalent:@"q"];
    appItem.submenu = appMenu;

    NSMenuItem *viewItem = [[NSMenuItem alloc] init];
    [main addItem:viewItem];
    NSMenu *viewMenu = [[NSMenu alloc] initWithTitle:@"显示"];
    [viewMenu addItemWithTitle:@"重新加载" action:@selector(reload) keyEquivalent:@"r"];
    [viewMenu addItemWithTitle:@"实际大小" action:@selector(zoomReset) keyEquivalent:@"0"];
    [viewMenu addItemWithTitle:@"放大" action:@selector(zoomIn) keyEquivalent:@"+"];
    [viewMenu addItemWithTitle:@"缩小" action:@selector(zoomOut) keyEquivalent:@"-"];
    [viewMenu addItem:[NSMenuItem separatorItem]];
    [viewMenu addItemWithTitle:@"进入全屏幕" action:@selector(toggleFullScreen:) keyEquivalent:@"f"];
    viewItem.submenu = viewMenu;

    NSMenuItem *editItem = [[NSMenuItem alloc] init];
    [main addItem:editItem];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"编辑"];
    [editMenu addItemWithTitle:@"拷贝" action:@selector(copy:) keyEquivalent:@"c"];
    [editMenu addItemWithTitle:@"全选" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = editMenu;

    NSApp.mainMenu = main;
}

- (void)reload { [self.webView reload]; }
- (void)zoomReset { self.webView.pageZoom = 1.0; }
- (void)zoomIn { self.webView.pageZoom = MIN(self.webView.pageZoom + 0.1, 3.0); }
- (void)zoomOut { self.webView.pageZoom = MAX(self.webView.pageZoom - 0.1, 0.4); }

#pragma mark - 导航

/// 站内链接留在应用里；站外链接交给系统浏览器。
/// 不加这一层的话，点一个外链会把看板顶掉，用户还得手动退回来。
- (void)webView:(WKWebView *)webView
    decidePolicyForNavigationAction:(WKNavigationAction *)action
                    decisionHandler:(void (^)(WKNavigationActionPolicy))handler {
    NSURL *url = action.request.URL;
    BOOL isLocal = [url.host isEqualToString:@"127.0.0.1"] || [url.host isEqualToString:@"localhost"];
    if (action.navigationType == WKNavigationTypeLinkActivated && !isLocal) {
        [[NSWorkspace sharedWorkspace] openURL:url];
        handler(WKNavigationActionPolicyCancel);
        return;
    }
    handler(WKNavigationActionPolicyAllow);
}

/// 页面加载失败（比如服务中途重启）时给出提示，而不是一片空白。
- (void)webView:(WKWebView *)webView
    didFailProvisionalNavigation:(WKNavigation *)navigation
                       withError:(NSError *)error {
    NSString *html = [NSString stringWithFormat:
        @"<html><head><meta charset='utf-8'><style>"
         "body{font:15px -apple-system;display:flex;align-items:center;"
         "justify-content:center;height:100vh;margin:0;color:#444}"
         ".b{text-align:center;line-height:1.9}"
         "code{background:#f2f2f7;padding:3px 7px;border-radius:5px;font-size:13px}"
         "</style></head><body><div class='b'>"
         "<h2>连不上后台服务</h2><p>%@</p>"
         "<p>确认服务在跑：<code>curl %@/api/health</code></p>"
         "<p>按 ⌘R 重试</p></div></body></html>",
        error.localizedDescription, kBaseURL];
    [webView loadHTMLString:html baseURL:nil];
}

@end

int main(void) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        AppDelegate *delegate = [[AppDelegate alloc] init];
        app.delegate = delegate;
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        [app run];
    }
    return 0;
}
