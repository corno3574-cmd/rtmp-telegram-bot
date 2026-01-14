import ffmpeg
import asyncio
import re
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
import os
from datetime import datetime, timezone, timedelta
import logging
import aiohttp
import aiohttp.web as web
import psutil
import json
import subprocess

# Configuration
BOT_TOKEN = '8302215619:AAEUDfdZDlWVpmThRgWV78mGiNlc_k9bggs'
CHAT_ID = os.getenv('CHAT_ID', '8302215619')  # Usando seu ID como chat padrão
MAX_CLIP_DURATION = 7200  # 2 horas (máximo permitido)
DEFAULT_CLIP_DURATION = 900  # 15 minutos por padrão
CLIP_DURATION = DEFAULT_CLIP_DURATION
RETRY_DELAY = 10    # Seconds to wait before retrying
MAX_RETRIES = 2    # Max retries per clip
VIDEO_RESOLUTION = '854:480'  # 480p (16:9)
PORT = int(os.getenv('PORT', 10000))  # Railway/Render PORT
GERMANY_TZ = timezone(timedelta(hours=1))  # Fuso horário da Alemanha (UTC+1, CET)

# List to store active RTMP links and running FFmpeg processes
active_rtmp_links = []
ffmpeg_processes = {}  # Map RTMP URL to subprocess

# Logging setup
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('rtmp_bot.log')
    ]
)
logger = logging.getLogger(__name__)

# Regex to detect RTMP URLs and extract stream ID
RTMP_REGEX = r'rtmp://[^\s]+'
STREAM_ID_REGEX = r'23331_sm_([^\?]+)'

# AioHTTP routes for port binding
async def health_check(request):
    logger.info("Health check endpoint hit")
    return web.Response(text="RTMP Telegram Bot is running", status=200)

def init_http_app():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    return app

async def start_http_server(app, port):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"HTTP server started on port {port}")
    return runner

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_time_germany = datetime.now(GERMANY_TZ).strftime('%Y-%m-%d %H:%M:%S')
    await update.message.reply_text(
        "🎬 RTMP Recorder Bot iniciado!\n"
        f"⏰ Horário Alemanha: {current_time_germany}\n\n"
        "📌 Comandos disponíveis:\n"
        "• Envie um link RTMP (rtmp://...) para gravação automática\n"
        "• /setrtmp <link> - Adicionar link manualmente\n"
        "• /list - Listar links ativos\n"
        "• /stop <link> - Parar gravação específica\n"
        "• /stopall - Parar todas as gravações\n"
        "• /clear - Limpar todos os links\n"
        f"• /setduration <segundos> - Definir duração dos clips (máx: {MAX_CLIP_DURATION}s = 2h)\n"
        "• /status - Status do bot e recursos\n"
        "• /testrtmp <link> - Testar stream RTMP\n"
        f"\n📝 Duração atual: {CLIP_DURATION} segundos ({CLIP_DURATION//60} minutos)"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_jobs = context.job_queue.jobs() if context.job_queue else []
    job_count = len(active_jobs)
    memory = psutil.virtual_memory()
    memory_used = memory.used / (1024 * 1024)
    memory_total = memory.total / (1024 * 1024)
    
    # Get disk info
    disk = psutil.disk_usage('.')
    disk_free = disk.free / (1024 * 1024 * 1024)  # GB
    
    current_time_germany = datetime.now(GERMANY_TZ).strftime('%Y-%m-%d %H:%M:%S')
    
    status_msg = (
        f"📊 Status do Bot:\n"
        f"⏰ Horário Alemanha: {current_time_germany}\n"
        f"• Links RTMP ativos: {len(active_rtmp_links)}\n"
        f"• Jobs em execução: {job_count}\n"
        f"• Duração do clip: {CLIP_DURATION}s ({CLIP_DURATION//60}min)\n"
        f"• Máximo permitido: {MAX_CLIP_DURATION}s ({MAX_CLIP_DURATION//3600}h)\n"
        f"• CPU: {psutil.cpu_percent()}%\n"
        f"• Memória: {memory_used:.1f}/{memory_total:.1f}MB\n"
        f"• Disco livre: {disk_free:.1f}GB\n"
        f"• Processos FFmpeg: {len(ffmpeg_processes)}"
    )
    await update.message.reply_text(status_msg)

async def test_rtmp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Uso: /testrtmp <rtmp_url>")
        return
    
    rtmp_url = context.args[0]
    if not rtmp_url.startswith('rtmp://'):
        await update.message.reply_text("❌ URL RTMP inválida. Deve começar com 'rtmp://'")
        return
    
    current_time = datetime.now(GERMANY_TZ).strftime('%H:%M:%S')
    await update.message.reply_text(f"🔍 Testando stream: {rtmp_url}\n⏰ Horário Alemanha: {current_time}")
    
    try:
        # Usando ffprobe para testar o stream
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,width,height',
            '-of', 'json',
            rtmp_url,
            '-timeout', '10000000'  # 10 segundos timeout
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        
        if process.returncode == 0:
            info = json.loads(stdout.decode())
            streams = info.get('streams', [])
            if streams:
                stream_info = streams[0]
                width = stream_info.get('width', 'N/A')
                height = stream_info.get('height', 'N/A')
                codec = stream_info.get('codec_name', 'N/A')
                
                await update.message.reply_text(
                    f"✅ Stream está ativo!\n"
                    f"📺 Resolução: {width}x{height}\n"
                    f"🎬 Codec: {codec}\n"
                    f"🔗 URL: {rtmp_url}"
                )
            else:
                await update.message.reply_text("⚠️ Stream encontrado, mas sem informações de vídeo")
        else:
            error_msg = stderr.decode('utf-8', errors='replace') if stderr else "Erro desconhecido"
            await update.message.reply_text(f"❌ Stream não acessível ou offline\nErro: {error_msg[:200]}")
            
    except asyncio.TimeoutError:
        await update.message.reply_text("⏱️ Timeout: Stream não respondeu em 15 segundos")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao testar stream: {str(e)}")

async def set_rtmp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Uso: /setrtmp <rtmp_url>")
        return
    
    rtmp_url = context.args[0]
    if not rtmp_url.startswith('rtmp://'):
        await update.message.reply_text("❌ URL RTMP inválida. Deve começar com 'rtmp://'")
        return
    
    if rtmp_url not in active_rtmp_links:
        active_rtmp_links.append(rtmp_url)
        current_time = datetime.now(GERMANY_TZ).strftime('%H:%M:%S')
        await update.message.reply_text(
            f"✅ Link RTMP adicionado: {rtmp_url}\n"
            f"⏰ Horário Alemanha: {current_time}\n"
            f"⏱️ Duração: {CLIP_DURATION//60} minutos"
        )
        
        if context.job_queue:
            context.job_queue.run_once(
                record_and_send_job, 
                0, 
                data={'rtmp_url': rtmp_url}, 
                name=rtmp_url
            )
        else:
            logger.error("Job queue not initialized")
            await update.message.reply_text("❌ Erro: Job queue não inicializada. Contacte o admin.")
    else:
        await update.message.reply_text("⚠️ Este link RTMP já está ativo.")

async def stop_rtmp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Uso: /stop <rtmp_url>")
        return
    
    rtmp_url = context.args[0]
    if rtmp_url in active_rtmp_links:
        active_rtmp_links.remove(rtmp_url)
        
        if rtmp_url in ffmpeg_processes:
            proc = ffmpeg_processes.pop(rtmp_url)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
                logger.info(f"Terminated FFmpeg process for {rtmp_url}")
            except asyncio.TimeoutError:
                proc.kill()
                logger.warning(f"Forced kill FFmpeg process for {rtmp_url}")
        
        current_time = datetime.now(GERMANY_TZ).strftime('%H:%M:%S')
        await update.message.reply_text(
            f"⏹️ Gravação parada para: {rtmp_url}\n"
            f"⏰ Horário Alemanha: {current_time}"
        )
    else:
        await update.message.reply_text("❌ Este link RTMP não está ativo.")

async def stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_rtmp_links
    
    if not active_rtmp_links and not ffmpeg_processes:
        await update.message.reply_text("ℹ️ Nenhuma gravação em andamento.")
        return
    
    stopped_count = 0
    for rtmp_url in list(ffmpeg_processes.keys()):
        proc = ffmpeg_processes.pop(rtmp_url)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            logger.info(f"Terminated FFmpeg process for {rtmp_url}")
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"Forced kill FFmpeg process for {rtmp_url}")
        stopped_count += 1
    
    active_rtmp_links = []
    
    if context.job_queue:
        jobs = context.job_queue.jobs()
        for job in jobs:
            job.schedule_removal()
    
    current_time = datetime.now(GERMANY_TZ).strftime('%H:%M:%S')
    await update.message.reply_text(
        f"🛑 Todas as gravações paradas\n"
        f"⏰ Horário Alemanha: {current_time}\n"
        f"📊 Processos terminados: {stopped_count}"
    )

async def set_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"⚠️ Uso: /setduration <segundos>\nMáximo permitido: {MAX_CLIP_DURATION} segundos (2 horas)")
        return
    
    try:
        global CLIP_DURATION
        new_duration = int(context.args[0])
        
        if new_duration < 10:
            await update.message.reply_text("❌ Duração mínima é 10 segundos.")
            return
        
        if new_duration > MAX_CLIP_DURATION:
            await update.message.reply_text(
                f"❌ Duração máxima permitida é {MAX_CLIP_DURATION} segundos (2 horas).\n"
                f"Você tentou: {new_duration} segundos."
            )
            return
        
        CLIP_DURATION = new_duration
        hours = new_duration // 3600
        minutes = (new_duration % 3600) // 60
        seconds = new_duration % 60
        
        time_str = ""
        if hours > 0:
            time_str += f"{hours}h "
        if minutes > 0:
            time_str += f"{minutes}min "
        if seconds > 0 or not time_str:
            time_str += f"{seconds}s"
        
        current_time = datetime.now(GERMANY_TZ).strftime('%H:%M:%S')
        await update.message.reply_text(
            f"✅ Duração do clip definida para: {new_duration} segundos\n"
            f"⏱️ Equivalente a: {time_str}\n"
            f"⏰ Horário Alemanha: {current_time}"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Por favor, forneça um número válido de segundos.")

async def list_rtmp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_time = datetime.now(GERMANY_TZ).strftime('%H:%M:%S')
    
    if active_rtmp_links:
        links_list = "📋 Links RTMP Ativos:\n\n"
        for i, link in enumerate(active_rtmp_links, 1):
            links_list += f"{i}. {link}\n"
        
        links_list += f"\n⏰ Horário Alemanha: {current_time}"
        links_list += f"\n⏱️ Duração: {CLIP_DURATION//60} minutos por clip"
        
        await update.message.reply_text(links_list)
    else:
        await update.message.reply_text(
            f"📭 Nenhum link RTMP ativo no momento.\n"
            f"⏰ Horário Alemanha: {current_time}"
        )

async def clear_rtmp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_rtmp_links
    
    if not active_rtmp_links and not ffmpeg_processes:
        await update.message.reply_text("ℹ️ Nenhum link para limpar.")
        return
    
    # Stop all FFmpeg processes
    stopped_count = 0
    for rtmp_url in list(ffmpeg_processes.keys()):
        proc = ffmpeg_processes.pop(rtmp_url)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            logger.info(f"Terminated FFmpeg process for {rtmp_url}")
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"Forced kill FFmpeg process for {rtmp_url}")
        stopped_count += 1
    
    # Clear active links
    cleared_count = len(active_rtmp_links)
    active_rtmp_links = []
    
    current_time = datetime.now(GERMANY_TZ).strftime('%H:%M:%S')
    await update.message.reply_text(
        f"🧹 Todos os links RTMP foram removidos\n"
        f"⏰ Horário Alemanha: {current_time}\n"
        f"📊 Links removidos: {cleared_count}\n"
        f"🛑 Processos terminados: {stopped_count}"
    )

async def detect_rtmp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message and not update.channel_post:
        logger.info("No message or channel post in update")
        return

    # Extract text from message, channel post, or forwarded content
    message = update.message or update.channel_post
    if not message:
        return

    text = (
        message.text or
        message.caption or
        (message.forward_origin.message.text if message.forward_origin and hasattr(message.forward_origin, 'message') and hasattr(message.forward_origin.message, 'text') else None) or
        (message.forward_origin.message.caption if message.forward_origin and hasattr(message.forward_origin, 'message') and hasattr(message.forward_origin.message, 'caption') else None)
    )
    
    if not text:
        logger.info(f"No text found in message from chat {message.chat_id}")
        return

    # Extract RTMP URLs
    rtmp_urls = re.findall(RTMP_REGEX, text)
    if not rtmp_urls:
        logger.info(f"No RTMP URLs found in message from chat {message.chat_id}")
        return

    current_time = datetime.now(GERMANY_TZ).strftime('%H:%M:%S')
    
    for rtmp_url in rtmp_urls:
        if rtmp_url not in active_rtmp_links:
            active_rtmp_links.append(rtmp_url)
            source = (
                f"channel post {message.forward_origin.channel_post.message_id}"
                if message.forward_origin and hasattr(message.forward_origin, 'channel_post')
                else "forwarded message" if message.forward_origin
                else f"chat {message.chat_id}"
            )
            
            logger.info(f"Detected RTMP link: {rtmp_url} from {source}")
            
            await message.reply_text(
                f"🎬 Link RTMP detectado!\n"
                f"🔗 {rtmp_url}\n"
                f"📝 Fonte: {source}\n"
                f"⏰ Horário Alemanha: {current_time}\n"
                f"⏱️ Duração: {CLIP_DURATION//60} minutos"
            )
            
            if context.job_queue:
                context.job_queue.run_once(
                    record_and_send_job, 
                    0, 
                    data={'rtmp_url': rtmp_url}, 
                    name=rtmp_url
                )
            else:
                logger.error("Job queue not initialized")
                await message.reply_text("❌ Erro: Job queue não inicializada. Contacte o admin.")

async def check_stream_health(rtmp_url):
    try:
        # Test with shorter timeout for health check
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'json',
            rtmp_url,
            '-timeout', '5000000'  # 5 segundos timeout
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        
        if process.returncode == 0:
            logger.info(f"Stream health check passed: {rtmp_url}")
            return True
        else:
            logger.error(f"Stream health check failed: {rtmp_url}")
            return False
            
    except asyncio.TimeoutError:
        logger.error(f"Stream health check timeout: {rtmp_url}")
        return False
    except Exception as e:
        logger.error(f"Stream health check error: {e}")
        return False

async def record_stream(rtmp_url, output_file):
    try:
        if not await check_stream_health(rtmp_url):
            logger.error(f"Stream {rtmp_url} não está ativo, pulando gravação.")
            return False
        
        logger.info(f"Iniciando gravação de {CLIP_DURATION} segundos de {rtmp_url}")
        
        cmd = [
            'ffmpeg', 
            '-i', rtmp_url,
            '-t', str(CLIP_DURATION),  # Limite de duração
            '-vcodec', 'libx264',
            '-acodec', 'aac',
            '-vf', 'scale=854:480',  # 480p
            '-preset', 'veryfast',
            '-b:v', '800k',
            '-b:a', '96k',
            '-crf', '28',
            '-f', 'mp4',
            '-movflags', '+faststart',  # Para streaming mais rápido
            output_file
        ]
        
        logger.debug(f"FFmpeg command: {' '.join(cmd)}")
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE
        )
        
        ffmpeg_processes[rtmp_url] = proc
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            if os.path.exists(output_file):
                file_size = os.path.getsize(output_file) / (1024 * 1024)
                logger.info(f"Gravação completa: {output_file}, tamanho: {file_size:.2f}MB")
                ffmpeg_processes.pop(rtmp_url, None)
                return True
            else:
                logger.error(f"Arquivo não criado: {output_file}")
                ffmpeg_processes.pop(rtmp_url, None)
                return False
        else:
            stderr_text = stderr.decode('utf-8', errors='replace') if stderr else "Sem stderr"
            logger.error(f"Erro FFmpeg: código {proc.returncode}, stderr: {stderr_text[:500]}")
            ffmpeg_processes.pop(rtmp_url, None)
            return False
            
    except Exception as e:
        logger.error(f"Falha na gravação: {e}")
        ffmpeg_processes.pop(rtmp_url, None)
        return False

async def send_to_telegram(output_file, rtmp_url):
    bot = Bot(token=BOT_TOKEN)
    
    if not os.path.exists(output_file):
        logger.error(f"Arquivo {output_file} não encontrado")
        try:
            await bot.send_message(
                chat_id=CHAT_ID, 
                text=f"❌ Falha ao gravar vídeo de {rtmp_url}: Arquivo não encontrado"
            )
        except TelegramError as e:
            logger.error(f"Falha ao enviar mensagem de erro: {e}")
        return False
    
    file_size = os.path.getsize(output_file) / (1024 * 1024)
    
    # Verificar tamanho do arquivo (limite do Telegram: 2GB para bots)
    if file_size > 1900:  # Deixar margem de segurança
        logger.error(f"Arquivo {output_file} muito grande: {file_size:.2f}MB")
        try:
            await bot.send_message(
                chat_id=CHAT_ID, 
                text=f"📦 Vídeo de {rtmp_url} muito grande: {file_size:.2f}MB (limite: ~1900MB)"
            )
        except TelegramError as e:
            logger.error(f"Falha ao enviar mensagem de erro: {e}"
