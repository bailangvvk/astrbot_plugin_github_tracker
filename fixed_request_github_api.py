async def request_github_api(self, session, url, method="GET"):
    """
    发送GitHub API请求，处理速率限制和认证
    
    Args:
        session: aiohttp会话对象
        url: API请求URL
        method: 请求方法，默认为GET
        
    Returns:
        成功时返回(True, 响应JSON)，失败时返回(False, 错误消息)
    """
    try:
        headers = await self.get_github_api_headers()
        
        # 检查是否接近速率限制
        if self.rate_limit["remaining"] < 5:
            now = int(time.time())
            if now < self.rate_limit["reset"]:
                wait_time = self.rate_limit["reset"] - now + 1
                logger.warning(f"接近API速率限制，等待 {wait_time} 秒至下次重置")
                await asyncio.sleep(wait_time)
        
        async with session.request(method, url, headers=headers) as resp:
            # 更新速率限制
            await self.update_rate_limit_from_response(resp)
            
            # 处理响应
            if resp.status == 200:
                return True, await resp.json()
            elif resp.status == 403 and "X-RateLimit-Remaining" in resp.headers and resp.headers["X-RateLimit-Remaining"] == "0":
                reset_time = int(resp.headers.get("X-RateLimit-Reset", 0))
                now = int(time.time())
                wait_time = max(0, reset_time - now) + 1
                error_msg = f"达到GitHub API速率限制，将在 {wait_time} 秒后重置"
                logger.warning(error_msg)
                return False, error_msg
            elif resp.status == 404:
                error_msg = "请求的资源不存在（404），可能是私有仓库或用户拼写错误"
                logger.error(f"{url}: {error_msg}")
                return False, error_msg
            else:
                try:
                    error_data = await resp.json()
                    error_msg = f"API请求失败，状态码: {resp.status}, 错误: {error_data.get('message', '未知错误')}"
                except:
                    error_msg = f"API请求失败，状态码: {resp.status}"
                logger.error(f"{url}: {error_msg}")
                return False, error_msg
    except aiohttp.ClientError as e:
        error_msg = f"API请求网络错误: {str(e)}"
        logger.exception(error_msg)
        return False, error_msg
    except asyncio.TimeoutError:
        error_msg = "API请求超时"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"API请求未知错误: {str(e)}"
        logger.exception(error_msg)
        return False, error_msg
