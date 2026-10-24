const fs = require('fs');

const path = 'c:/Users/HP/sara-ai-platform/frontend/src/app/hub/ai-web-scraper/page.tsx';
let c = fs.readFileSync(path, 'utf8');

// 1. Inject the fonts and css into the file
const heroStyles = `
/* injected hero styles */
@import url('https://fonts.googleapis.com/css2?family=Geist&display=swap');
@import url('https://api.fontshare.com/v2/css?f[]=general-sans@400,500,600,700&display=swap');

.hero-wrapper {
  --bg-color: hsl(260, 87%, 3%);
  --fg-color: hsl(40, 6%, 95%);
  --sub-color: hsl(40, 6%, 82%);
  background-color: var(--bg-color);
  color: var(--fg-color);
  font-family: 'Geist', sans-serif;
}
.hero-wrapper h1 {
  font-family: 'General Sans', sans-serif;
}

.liquid-glass {
  background: rgba(255, 255, 255, 0.01);
  background-blend-mode: luminosity;
  backdrop-filter: blur(4px);
  border: none;
  box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.1);
  position: relative;
  overflow: hidden;
}
.liquid-glass::before {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: inherit;
  padding: 1.4px;
  background: linear-gradient(180deg, rgba(255,255,255,0.45) 0%, rgba(255,255,255,0.15) 20%, rgba(255,255,255,0) 40%, rgba(255,255,255,0) 60%, rgba(255,255,255,0.15) 80%, rgba(255,255,255,0.45) 100%);
  -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  -webkit-mask-composite: xor;
  mask-composite: exclude;
  pointer-events: none;
}

.marquee-container {
  display: flex;
  overflow: hidden;
  user-select: none;
  gap: 4rem;
}
.marquee-content {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 4rem;
  animation: marquee-scroll 20s linear infinite;
}
@keyframes marquee-scroll {
  from { transform: translateX(0%); }
  to { transform: translateX(-100%); }
}

.heroSecondary {
  background: rgba(255,255,255,0.1);
  border: 1px solid rgba(255,255,255,0.2);
  color: var(--fg-color);
  border-radius: 9999px;
  transition: all 0.2s;
  font-weight: 500;
}
.heroSecondary:hover {
  background: rgba(255,255,255,0.15);
}
`;

// Inject into CSS_GLOBALS
c = c.replace('const CSS_GLOBALS = `', 'const CSS_GLOBALS = `' + heroStyles);

// 2. Add Hero component before ScrapeworkPage
const heroComponent = `
function HeroSection() {
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const vid = videoRef.current;
    if (!vid) return;
    
    // Custom JS-controlled fade loop
    const handlePlay = () => {
       vid.style.transition = 'opacity 0.5s ease-in';
       vid.style.opacity = '1';
    };
    
    const handleTimeUpdate = () => {
       if (vid.duration && vid.currentTime >= vid.duration - 0.5) {
          vid.style.transition = 'opacity 0.5s ease-out';
          vid.style.opacity = '0';
       }
    };
    
    const handleEnded = () => {
       vid.style.transition = 'none';
       vid.style.opacity = '0';
       setTimeout(() => {
          vid.play().catch(()=>{});
       }, 100);
    };

    vid.addEventListener('playing', handlePlay);
    vid.addEventListener('timeupdate', handleTimeUpdate);
    vid.addEventListener('ended', handleEnded);
    
    return () => {
       vid.removeEventListener('playing', handlePlay);
       vid.removeEventListener('timeupdate', handleTimeUpdate);
       vid.removeEventListener('ended', handleEnded);
    };
  }, []);

  const logos = ['Vortex', 'Nimbus', 'Prysma', 'Cirrus', 'Kynder', 'Halcyn'];

  const scrollToScraper = () => {
    document.getElementById('scraper-app')?.scrollIntoView({ behavior: 'smooth' });
  };

  return (
    <div className="hero-wrapper relative min-h-screen flex flex-col overflow-hidden">
      {/* Background Video */}
      <div className="absolute inset-0 overflow-hidden w-full h-full -z-20">
        <video 
          ref={videoRef}
          src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260328_065045_c44942da-53c6-4804-b734-f9e07fc22e08.mp4"
          className="absolute inset-0 w-full h-full object-cover opacity-0"
          autoPlay muted playsInline
        />
      </div>

      {/* Blurred overlay shape */}
      <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[984px] h-[527px] opacity-90 bg-gray-950 blur-[82px] pointer-events-none -z-10 rounded-full" />

      {/* Navbar */}
      <nav className="w-full py-5 px-8 flex flex-row items-center justify-between z-10 relative">
        <div className="flex items-center">
          <img src="/assets/logo.png" alt="Logo" style={{ height: '32px' }} onError={(e) => { e.currentTarget.style.display='none'; }} />
        </div>
        <div className="flex items-center gap-6">
          <button className="text-[var(--fg-color)] opacity-90 hover:opacity-100 transition-opacity flex items-center gap-1 font-medium">Features <IconChevronDown size={14}/></button>
          <button className="text-[var(--fg-color)] opacity-90 hover:opacity-100 transition-opacity font-medium">Solutions</button>
          <button className="text-[var(--fg-color)] opacity-90 hover:opacity-100 transition-opacity font-medium">Plans</button>
          <button className="text-[var(--fg-color)] opacity-90 hover:opacity-100 transition-opacity flex items-center gap-1 font-medium">Learning <IconChevronDown size={14}/></button>
        </div>
        <div>
          <button className="heroSecondary px-4 py-2 text-sm">Sign Up</button>
        </div>
      </nav>
      {/* Divider */}
      <div className="w-full h-[1px] bg-gradient-to-r from-transparent via-[var(--fg-color)] opacity-20 to-transparent mt-[3px] z-10 relative" />

      {/* Hero Content */}
      <div className="flex-1 flex flex-col items-center justify-center text-center z-10 relative px-4">
        <h1 className="text-[120px] md:text-[220px] font-normal leading-[1.02] tracking-[-0.024em]">
          Power <span className="bg-clip-text text-transparent" style={{ backgroundImage: 'linear-gradient(to left, #6366f1, #a855f7, #fcd34d)' }}>AI</span>
        </h1>
        <p className="text-[var(--sub-color)] text-lg leading-8 max-w-md mt-[9px] opacity-80">
          The most powerful AI ever deployed<br/>in talent acquisition
        </p>
        <button onClick={scrollToScraper} className="heroSecondary px-[29px] py-[24px] mt-[25px] text-lg">
          Schedule a Consult
        </button>
      </div>

      {/* Logo Marquee */}
      <div className="w-full z-10 relative pb-10">
        <div className="max-w-5xl mx-auto flex flex-col md:flex-row items-center gap-12 px-6">
          <div className="text-[var(--fg-color)] opacity-50 text-sm whitespace-nowrap text-center md:text-left leading-relaxed">
            Relied on by brands<br/>across the globe
          </div>
          <div className="marquee-container flex-1 mask-image: linear-gradient(to right, transparent, black 10%, black 90%, transparent)">
            <div className="marquee-content">
              {[...logos, ...logos, ...logos].map((name, i) => (
                <div key={i} className="flex items-center gap-3">
                  <div className="liquid-glass w-8 h-8 rounded-lg flex items-center justify-center font-bold text-[14px]">
                    {name[0]}
                  </div>
                  <span className="text-base font-semibold text-[var(--fg-color)]">{name}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

`;

c = c.replace('export default function ScrapeworkPage() {', heroComponent + 'export default function ScrapeworkPage() {');

// 3. Wrap main return with a scroll container and mount the scraper below it
const retIdx = c.lastIndexOf('return (\n    <div className="min-h-screen bg-[#0B0E14]');
const oldRetStr = 'return (\n    <div className="min-h-screen bg-[#0B0E14] text-slate-200 antialiased">';

const newRetStr = `return (
    <div className="flex flex-col w-full h-full overflow-y-auto">
      <HeroSection />
      <div id="scraper-app" className="min-h-screen bg-[#0B0E14] text-slate-200 antialiased relative z-20">`;

c = c.replace(oldRetStr, newRetStr);

// Now find the closing div of the old return and add one more closing div
const closingIdx = c.lastIndexOf('</div>\n  );\n}');
c = c.substring(0, closingIdx) + '</div>\n    </div>\n  );\n}';

fs.writeFileSync('c:/Users/HP/sara-ai-platform/frontend/src/app/hub/ai-web-scraper/page.tsx', c, 'utf8');
console.log('Success injected hero section!');
