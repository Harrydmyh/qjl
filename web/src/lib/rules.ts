/**
 * 业务判据（和原来单文件版里的实现一一对应）。
 * 都是纯函数，不碰网络和 DOM，方便单独测。
 */
import type { Activity, Category } from '../types';

export const CAT_LABEL: Record<Category, string> = {
  new: '新活动',
  followed: '已跟过',
  reopen: '可复开',
};

/** 能不能开团：followEarning 非 null（后端给了 hasFollowRelation 就优先用它） */
export function canOpenAct(a: Activity): boolean {
  if (a.hasFollowRelation !== undefined && a.hasFollowRelation !== null) return !!a.hasFollowRelation;
  return (a.followEarning || 0) > 0;
}

/** 同款 5 天内已开（后端算好的，不丢弃、仍显示，但不参与开团） */
export function isRecentlyOpened(a: Activity): boolean {
  return !!a.recentlyOpened;
}

export function isAlreadyFollowed(a: Activity): boolean {
  return !!a.alreadyFollowed;
}

/** 目前不能开：不支持跟团，或同款在冷却里 */
export function isOpenBlocked(a: Activity): boolean {
  return !canOpenAct(a) || isRecentlyOpened(a);
}

/**
 * 开团面板的默认勾选。
 * 「已跟过」= 平台记录里你已经开过这个团 —— 这是工具唯一能看到的「我自己开过」的证据
 * （它看不到你自己在群接龙里开的团），所以**默认不勾**，避免同一款被反复开；
 * 想再开手动勾上。
 */
export function isDefaultOpenPick(a: Activity): boolean {
  return !isOpenBlocked(a) && !isAlreadyFollowed(a);
}

export function cooldownText(a: Activity): string {
  const d = a.daysSinceOpen;
  return d == null ? '同款 5 天内已开' : `同款 ${d} 天前已开`;
}

/** 冷却中的用跟团佣金（复开活动平台不再提供，退回供货佣金） */
export function commissionOf(a: Activity): number {
  return a.category === 'reopen' ? a.earning || 0 : a.followEarning || 0;
}

export function commissionLabel(a: Activity): string {
  return a.category === 'reopen' ? '供货佣金' : '跟团佣金';
}
